# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Volume drivers for libvirt."""

import os
import time

from nova import exception
from nova import flags
from nova.openstack.common import log as logging
from nova import utils
from nova.virt.libvirt import config
from nova.virt.libvirt import utils as virtutils
from nova.api.ec2 import ec2utils

LOG = logging.getLogger(__name__)
FLAGS = flags.FLAGS
flags.DECLARE('num_iscsi_scan_tries', 'nova.volume.driver')
flags.DECLARE('libvirt_iscsi_use_multipath', 'nova.volume.driver')

class LibvirtVolumeDriver(object):
    """Base class for volume drivers."""
    def __init__(self, connection):
        self.connection = connection

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume. Returns xml for libvirt."""
        conf = config.LibvirtConfigGuestDisk()
        conf.source_type = "block"
        conf.driver_name = virtutils.pick_disk_driver_name(is_block_dev=True)
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.source_path = connection_info['data']['device_path']
        conf.target_dev = mount_device
        conf.target_bus = "virtio"
        conf.serial = connection_info.get('serial')

        try:
            volume_id = connection_info['data']['volume_id']

            # TODO: ec2_volume_id have to be computed from FLAGS.volume_name_template
            local_volume_ec2id = "/dev/%s/%s" % (FLAGS.volume_group, ec2utils.id_to_ec2_vol_id(volume_id))

            local_volume_uuid = "/dev/%s/%s" % (FLAGS.volume_group, FLAGS.volume_name_template)
            local_volume_uuid = local_volume_uuid % volume_id
            is_local_volume_ec2id = os.path.islink(local_volume_ec2id)
            is_local_volume_uuid = os.path.islink(local_volume_uuid)

            if is_local_volume_uuid:
                 conf.source_path = local_volume_uuid
            elif is_local_volume_ec2id:
                 conf.source_path = local_volume_ec2id
            else:
                 LOG.debug("Attaching device %s as %s" % (conf.source_path, mount_device))
        except KeyError as e:
             LOG.debug("Attaching device failed, because volume_id is not present. Shitty coding, that needs to be worked-around, in order for unit tests to pass")

        return conf

    def disconnect_volume(self, connection_info, mount_device):
        """Disconnect the volume"""
        pass


class LibvirtFakeVolumeDriver(LibvirtVolumeDriver):
    """Driver to attach Network volumes to libvirt."""

    def connect_volume(self, connection_info, mount_device):
        conf = config.LibvirtConfigGuestDisk()
        conf.source_type = "network"
        conf.driver_name = "qemu"
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.source_protocol = "fake"
        conf.source_host = "fake"
        conf.target_dev = mount_device
        conf.target_bus = "virtio"
        conf.serial = connection_info.get('serial')
        return conf


class LibvirtNetVolumeDriver(LibvirtVolumeDriver):
    """Driver to attach Network volumes to libvirt."""

    def connect_volume(self, connection_info, mount_device):
        conf = config.LibvirtConfigGuestDisk()
        conf.source_type = "network"
        conf.driver_name = virtutils.pick_disk_driver_name(is_block_dev=False)
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.source_protocol = connection_info['driver_volume_type']
        conf.source_host = connection_info['data']['name']
        conf.target_dev = mount_device
        conf.target_bus = "virtio"
        conf.serial = connection_info.get('serial')
        netdisk_properties = connection_info['data']
        if netdisk_properties.get('auth_enabled'):
            conf.auth_username = netdisk_properties['auth_username']
            conf.auth_secret_type = netdisk_properties['secret_type']
            conf.auth_secret_uuid = netdisk_properties['secret_uuid']
        return conf


class LibvirtISCSIVolumeDriver(LibvirtVolumeDriver):
    """Driver to attach Network volumes to libvirt."""

    def _run_iscsiadm(self, iscsi_properties, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = utils.execute('iscsiadm', '-m', 'node', '-T',
                                   iscsi_properties['target_iqn'],
                                   '-p', iscsi_properties['target_portal'],
                                   *iscsi_command, run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %s: stdout=%s stderr=%s" %
                  (iscsi_command, out, err))
        return (out, err)

    def _iscsiadm_update(self, iscsi_properties, property_key, property_value,
                         **kwargs):
        iscsi_command = ('--op', 'update', '-n', property_key,
                         '-v', property_value)
        return self._run_iscsiadm(iscsi_properties, iscsi_command, **kwargs)

    def _get_target_portals_from_iscsiadm_output(self, output):
        return [line.split()[0] for line in output.splitlines()]

    @utils.synchronized('connect_volume')
    def connect_volume(self, connection_info, mount_device):
        """Attach the volume to instance_name"""
        iscsi_properties = connection_info['data']

        libvirt_iscsi_use_multipath = FLAGS.libvirt_iscsi_use_multipath

        if libvirt_iscsi_use_multipath:
            #multipath installed, discovering other targets if available
            #multipath should be configured on the nova-compute node,
            #in order to fit storage vendor
            out = self._run_iscsiadm_bare(['-m',
                                          'discovery',
                                          '-t',
                                          'sendtargets',
                                          '-p',
                                          iscsi_properties['target_portal']],
                                          check_exit_code=[0, 255])[0] \
                or ""

            for ip in self._get_target_portals_from_iscsiadm_output(out):
                props = iscsi_properties.copy()
                props['target_portal'] = ip
                self._connect_to_iscsi_portal(props)

            self._rescan_iscsi()
        else:
            self._connect_to_iscsi_portal(iscsi_properties)

        host_device = ("/dev/disk/by-path/ip-%s-iscsi-%s-lun-%s" %
                       (iscsi_properties['target_portal'],
                        iscsi_properties['target_iqn'],
                        iscsi_properties.get('target_lun', 0)))

        # The /dev/disk/by-path/... node is not always present immediately
        # TODO(justinsb): This retry-with-delay is a pattern, move to utils?
        tries = 0
        while not os.path.exists(host_device):
            if tries >= FLAGS.num_iscsi_scan_tries:
                raise exception.NovaException(_("iSCSI device not found at %s")
                                              % (host_device))

            LOG.warn(_("ISCSI volume not yet found at: %(mount_device)s. "
                       "Will rescan & retry.  Try number: %(tries)s") %
                     locals())

            # The rescan isn't documented as being necessary(?), but it helps
            self._run_iscsiadm(iscsi_properties, ("--rescan",))

            tries = tries + 1
            if not os.path.exists(host_device):
                time.sleep(tries ** 2)

        if tries != 0:
            LOG.debug(_("Found iSCSI node %(mount_device)s "
                        "(after %(tries)s rescans)") %
                      locals())

        if libvirt_iscsi_use_multipath:
            #we use the multipath device instead of the single path device
            self._rescan_multipath()
            multipath_device = self._get_multipath_device_name(host_device)
            if multipath_device is not None:
                host_device = multipath_device

        connection_info['data']['device_path'] = host_device
        sup = super(LibvirtISCSIVolumeDriver, self)
        return sup.connect_volume(connection_info, mount_device)

    @utils.synchronized('connect_volume')
    def disconnect_volume(self, connection_info, mount_device):
        """Detach the volume from instance_name"""
        iscsi_properties = connection_info['data']
        multipath_device = None
        if FLAGS.libvirt_iscsi_use_multipath:
            host_device = ("/dev/disk/by-path/ip-%s-iscsi-%s-lun-%s" %
                           (iscsi_properties['target_portal'],
                            iscsi_properties['target_iqn'],
                            iscsi_properties.get('target_lun', 0)))
            multipath_device = self._get_multipath_device_name(host_device)

        sup = super(LibvirtISCSIVolumeDriver, self)
        sup.disconnect_volume(connection_info, mount_device)

        if FLAGS.libvirt_iscsi_use_multipath and multipath_device:
            return self._disconnect_volume_multipath_iscsi(iscsi_properties,
                                                           multipath_device)

        # NOTE(vish): Only disconnect from the target if no luns from the
        #             target are in use.
        device_prefix = ("/dev/disk/by-path/ip-%s-iscsi-%s-lun-" %
                         (iscsi_properties['target_portal'],
                          iscsi_properties['target_iqn']))
        devices = self.connection.get_all_block_devices()
        devices = [dev for dev in devices if dev.startswith(device_prefix)]
        if not devices:
            self._disconnect_from_iscsi_portal(iscsi_properties)

    def _remove_multipath_device_descriptor(self, disk_descriptor):
        disk_descriptor = disk_descriptor.replace('/dev/mapper/', '')
        try:
            self._run_multipath(['-f', disk_descriptor],
                                check_exit_code=[0, 1])
        except exception.ProcessExecutionError as exc:
            # Because not all cinder drivers need to remove the dev mapper,
            # here just logs a warning to avoid affecting those drivers in
            # exceptional cases.
            LOG.warn(_('Failed to remove multipath device descriptor '
                       '%(dev_mapper)s. Exception message: %(msg)s')
                     % {'dev_mapper': disk_descriptor,
                        'msg': exc.message})

    def _disconnect_volume_multipath_iscsi(self, iscsi_properties,
                                           multipath_device):
        self._rescan_iscsi()
        self._rescan_multipath()
        block_devices = self.connection.get_all_block_devices()
        devices = []
        for dev in block_devices:
            if "/mapper/" in dev:
                devices.append(dev)
            else:
                mpdev = self._get_multipath_device_name(dev)
                if mpdev:
                    devices.append(mpdev)

        if not devices:
            # disconnect if no other multipath devices
            self._disconnect_mpath(iscsi_properties)
            return

        other_iqns = [self._get_multipath_iqn(device)
                      for device in devices]

        if iscsi_properties['target_iqn'] not in other_iqns:
            # disconnect if no other multipath devices with same iqn
            self._disconnect_mpath(iscsi_properties)
            return

        # else do not disconnect iscsi portals,
        # as they are used for other luns,
        # just remove multipath mapping device descriptor
        self._remove_multipath_device_descriptor(multipath_device)
        return

    def _connect_to_iscsi_portal(self, iscsi_properties):
        # NOTE(vish): If we are on the same host as nova volume, the
        #             discovery makes the target so we don't need to
        #             run --op new. Therefore, we check to see if the
        #             target exists, and if we get 255 (Not Found), then
        #             we run --op new. This will also happen if another
        #             volume is using the same target.
        try:
            self._run_iscsiadm(iscsi_properties, ())
        except exception.ProcessExecutionError as exc:
            # iscsiadm returns 21 for "No records found" after version 2.0-871
            if exc.exit_code in [21, 255]:
                self._run_iscsiadm(iscsi_properties, ('--op', 'new'))
            else:
                raise

        if iscsi_properties.get('auth_method'):
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.authmethod",
                                  iscsi_properties['auth_method'])
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.username",
                                  iscsi_properties['auth_username'])
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.password",
                                  iscsi_properties['auth_password'])

        #duplicate logins crash iscsiadm after load,
        #so we scan active sessions to see if the node is logged in.
        out = self._run_iscsiadm_bare(["-m", "session"],
                                      run_as_root=True,
                                      check_exit_code=[0, 21, 1])[0] or ""

        portals = [{'portal': p.split(" ")[2], 'iqn': p.split(" ")[3]}
                   for p in out.splitlines() if p.startswith("tcp:")]

        stripped_portal = iscsi_properties['target_portal'].split(",")[0]
        if len(portals) == 0 or len([s for s in portals
                                     if stripped_portal ==
                                     s['portal'].split(",")[0]
                                     and
                                     s['iqn'] ==
                                     iscsi_properties['target_iqn']]
                                    ) == 0:
            try:
                self._run_iscsiadm(iscsi_properties,
                                   ("--login",),
                                   check_exit_code=[0, 255])
            except exception.ProcessExecutionError as err:
                #as this might be one of many paths,
                #only set successfull logins to startup automatically
                if err.exit_code in [15]:
                    self._iscsiadm_update(iscsi_properties,
                                          "node.startup",
                                          "automatic")
                    return

            self._iscsiadm_update(iscsi_properties,
                                  "node.startup",
                                  "automatic")

    def _disconnect_from_iscsi_portal(self, iscsi_properties):
        self._iscsiadm_update(iscsi_properties, "node.startup", "manual",
                              check_exit_code=[0, 21, 255])
        self._run_iscsiadm(iscsi_properties, ("--logout",),
                           check_exit_code=[0, 21, 255])
        self._run_iscsiadm(iscsi_properties, ('--op', 'delete'),
                           check_exit_code=[0, 21, 255])

    def _get_multipath_device_name(self, single_path_device):
        device = os.path.realpath(single_path_device)
        out = self._run_multipath(['-ll',
                                  device],
                                  check_exit_code=[0, 1])[0]
        mpath_line = [line for line in out.splitlines()
                      if "scsi_id" not in line]  # ignore udev errors
        if len(mpath_line) > 0 and len(mpath_line[0]) > 0:
            return "/dev/mapper/%s" % mpath_line[0].split(" ")[0]

        return None

    def _get_iscsi_devices(self):
        return [entry for entry in list(os.walk('/dev/disk/by-path'))[0][-1]
                if entry.startswith("ip-")]

    def _disconnect_mpath(self, iscsi_properties):
        entries = self._get_iscsi_devices()
        ips = [ip.split("-")[1] for ip in entries
               if iscsi_properties['target_iqn'] in ip]
        for ip in ips:
            props = iscsi_properties.copy()
            props['target_portal'] = ip
            self._disconnect_from_iscsi_portal(props)

        self._rescan_multipath()

    def _get_multipath_iqn(self, multipath_device):
        entries = self._get_iscsi_devices()
        for entry in entries:
            entry_real_path = os.path.realpath("/dev/disk/by-path/%s" % entry)
            entry_multipath = self._get_multipath_device_name(entry_real_path)
            if entry_multipath == multipath_device:
                return entry.split("iscsi-")[1].split("-lun")[0]
        return None

    def _run_iscsiadm_bare(self, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = utils.execute('iscsiadm',
                                   *iscsi_command,
                                   run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %s: stdout=%s stderr=%s" %
                  (iscsi_command, out, err))
        return (out, err)

    def _run_multipath(self, multipath_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = utils.execute('multipath',
                                   *multipath_command,
                                   run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("multipath %s: stdout=%s stderr=%s" %
                  (multipath_command, out, err))
        return (out, err)

    def _rescan_iscsi(self):
        self._run_iscsiadm_bare(('-m', 'node', '--rescan'),
                                check_exit_code=[0, 1, 21, 255])
        self._run_iscsiadm_bare(('-m', 'session', '--rescan'),
                                check_exit_code=[0, 1, 21, 255])

    def _rescan_multipath(self):
        self._run_multipath(['-r'], check_exit_code=[0, 1, 21])

