#
# UEFI validation/integration tests
#
# Copyright (C) 2019 Canonical, Ltd.
# Author: Mathieu Trudel-Lapierre <mathieu.trudel-lapierre@canonical.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import shutil
import stat
import math
import subprocess
import tempfile
from time import sleep
import unittest


class UEFINotAvailable(Exception):
    """Exception class for unavailable UEFI features"""
    def __init__(self, feature=None, arch=None, details=None):
        self.message = "UEFI is not available"
        if arch and feature:
            self.message = "%s is not available on %s" % (feature, arch)
        elif feature:
            self.message = "%s is not available" % feature
        if details:
            self.message = self.message + ": %s" % details

    def __str__(self):
        return repr(self.message)

class UEFITestsBase(unittest.TestCase):
    '''
    Common functionality for shim test cases
    '''

    @classmethod
    def setUpClass(klass):
        klass.arch_machine = os.uname().machine
        klass.arch_suffix = ''
        klass.grub_arch = ''
        klass.qemu_arch = ''
        if klass.arch_machine == 'x86_64':
            klass.image_arch = 'amd64'
            klass.arch_suffix = 'x64'
            klass.grub_arch = 'x86_64-efi'
            klass.qemu_arch = 'qemu-system-x86_64'
        elif klass.arch_machine == 'aarch64':
            klass.image_arch = 'arm64'
            klass.arch_suffix = 'aa64'
            klass.grub_arch = 'arm64-efi'
            klass.qemu_arch = 'qemu-system-aarch64'
        else:
            raise UEFINotAvailable(feature='any UEFI Shim features', arch=klass.arch_machine)

        # Base paths for the ESP.
        klass.uefi_base_dir = os.path.join('/', 'boot', 'efi', 'EFI')
        klass.uefi_boot_dir = os.path.join(klass.uefi_base_dir, 'BOOT')
        klass.uefi_install_dir = os.path.join(klass.uefi_base_dir, 'debian')

        # CAs for signature validation (not yet)
        # klass.ca = os.path.join('/usr/share/grub', 'debian-uefi-ca.crt')

        # Shim paths
        klass.shim_pkg_dir = os.path.join('/', 'usr', 'lib', 'shim')
        klass.unsigned_shim_path = os.path.join(klass.shim_pkg_dir, 'shim%s.efi' % klass.arch_suffix)
        klass.signed_shim_path = os.path.join(klass.shim_pkg_dir, 'shim%s.efi.signed' % klass.arch_suffix)
        klass.installed_shim = os.path.join(klass.uefi_install_dir, 'shim%s.efi' % klass.arch_suffix)
        klass.removable_shim = os.path.join(klass.uefi_boot_dir, 'boot%s.efi' % klass.arch_suffix)

        # GRUB paths
        klass.grub_pkg_dir = os.path.join('/', 'usr', 'lib', 'grub', "%s-signed" % klass.grub_arch)
        klass.signed_grub_path = os.path.join(klass.grub_pkg_dir, 'grub%s.efi.signed' % klass.arch_suffix)
        klass.installed_grub = os.path.join(klass.uefi_install_dir, 'grub%s.efi' % klass.arch_suffix)

        # OMVF paths
        if klass.arch_machine == 'x86_64':
            klass.uefi_code_path = '/usr/share/OVMF/OVMF_CODE_4M.ms.fd'
            klass.uefi_vars_path = '/usr/share/OVMF/OVMF_VARS_4M.ms.fd'
            klass.uefi_qemu_extra = [ '-machine', 'q35,smm=on' ]
        elif klass.arch_machine == 'aarch64':
            klass.uefi_code_path = '/usr/share/AAVMF/AAVMF_CODE.fd'
            klass.uefi_vars_path = '/usr/share/AAVMF/AAVMF_VARS.fd'
            klass.uefi_qemu_extra = []

        subprocess.run(['modprobe', 'nbd'])

    @classmethod
    def tearDownClass(klass):
        pass

    def tearDown(self):
        pass

    def setUp(self):
        pass


    #
    # Internal implementation details
    #

    @classmethod
    def poll_text(klass, logpath, string, timeout=50):
        '''Poll log file for a given string with a timeout.

        Timeout is given in deciseconds.
        '''
        log = ''
        while timeout > 0:
            if os.path.exists(logpath):
                break
            timeout -= 1
            sleep(0.1)
        assert timeout > 0, 'Timed out waiting for file %s to appear' % logpath

        with open(logpath) as f:
            while timeout > 0:
                line = f.readline()
                if line:
                    log += line
                    if string in line:
                        break
                    continue
                timeout -= 1
                sleep(0.1)

        assert timeout > 0, 'Timed out waiting for "%s":\n------------\n%s\n-------\n' % (string, log)


    def assertSignatureOK(self, expected_signature, binary):
        result = subprocess.check_call(['sbverify', '--cert', expected_signature, binary])
        self.assertEquals(0, result)


    def assertBoots(self, vm=None):
        '''Assert that the VM is booted and ready for use'''
        self.assertTrue(vm.ready())


DEFAULT_METADATA = 'instance-id: nocloud\nlocal-hostname: autopkgtest\n'

DEFAULT_USERDATA = """#cloud-config
locale: en_US.UTF-8
password: debian
chpasswd: { expire: False }
ssh_pwauth: True
manage_etc_hosts: True
runcmd:
 - (while [ ! -e /var/lib/cloud/instance/boot-finished ]; do sleep 1; done;
    touch /TESTOK;
    shutdown -P now) &
"""

#
# VM management tools
#
class UEFIVirtualMachine(UEFITestsBase):

    def __init__(self, base=None, arch=None):
        self.autopkgtest_dir = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.autopkgtest_dir.name, 'img'))
        self.arch = arch
        release = subprocess.run(['lsb_release','-c','-s'], capture_output=True, check=True)
        self.release = release.stdout.strip().decode('utf-8')
        release_number = subprocess.run(['lsb_release','-r','-s'], capture_output=True, check=True).stdout.strip().decode('utf-8')
        self.release_number = None
        try:
           self.release_number = int(math.floor(float(release_number)))
        except:
           if(self.release == 'sid'):
               self.release_number = 'sid'
           else:
               alias = subprocess.run(['distro-info','--alias', self.release], capture_output=True, check=True).stdout.strip().decode('utf-8')
               number_distro = subprocess.run(['distro-info','-r', '--%s' % (alias)], capture_output=True, check=True).stdout.strip().decode('utf-8')
               self.release_number = int(math.floor(float(number_distro)))
        self.path = tempfile.mkstemp(dir=self.autopkgtest_dir.name)[1]
        if not base:
            subprocess.run(['wget',
                            'https://cloud.debian.org/images/cloud/%s/daily/latest/debian-%s-genericcloud-%s-daily.qcow2'
                            % (self.release, self.release_number, self.arch),
                            '-O', '%s/base.img' % self.autopkgtest_dir.name], check = True)
        else:
            self.arch = base.arch
            shutil.copy(base.path, os.path.join(self.autopkgtest_dir.name, 'base.img'))
        shutil.copy(os.path.join(self.autopkgtest_dir.name, 'base.img'), self.path)
        shutil.copy("%s" % self.uefi_vars_path, "%s.VARS.fd" % self.path) 

    def _mount(self):
        subprocess.run(['qemu-nbd', '--connect=/dev/nbd0', self.path])
        # nbd doesn't show instantly...
        sleep(1)
        subprocess.run(['mount', '/dev/nbd0p1', os.path.join(self.autopkgtest_dir.name, 'img')])
        subprocess.run(['mount', '/dev/nbd0p15', os.path.join(self.autopkgtest_dir.name, 'img', 'boot/efi')])

    def _unmount(self):
        subprocess.run(['umount', '/dev/nbd0p15'])
        subprocess.run(['umount', '/dev/nbd0p1'])
        subprocess.run(['qemu-nbd', '--disconnect', '/dev/nbd0'])

    def prepare(self):
        with open(os.path.join(self.autopkgtest_dir.name, 'meta-data'), 'w') as f:
            f.write(DEFAULT_METADATA)
        with open(os.path.join(self.autopkgtest_dir.name, 'user-data'), 'w') as f:
            f.write(DEFAULT_USERDATA)
        with open(os.path.join(self.autopkgtest_dir.name, 'network-config'), 'w') as f:
            f.write('')

        subprocess.run(['genisoimage', '-output', 'cloud-init.seed',
                        '-volid', 'cidata', '-joliet', '-rock',
                        '-quiet', 'user-data', 'meta-data', 'network-config'],
                       cwd=self.autopkgtest_dir.name)

    def list(self, path='/etc/'):
        self._mount()
        subprocess.run(['ls', '-l',  os.path.join(self.autopkgtest_dir.name, 'img', path)])
        self._unmount()

    def update(self, src=None, dst=None):
        self._mount()
        try:
            os.makedirs(os.path.join(self.autopkgtest_dir.name, 'img', os.path.dirname(dst)))
        except FileExistsError:
            pass
        shutil.copy(src, os.path.join(self.autopkgtest_dir.name, 'img', dst))
        self._unmount()

    def run(self):
        self.prepare()
        # start qemu-system-$arch, output log to serial and capture to variable
        subprocess.run([self.qemu_arch] + self.uefi_qemu_extra + [
                        '-m', '1024', '-nographic',
                        '-serial', 'mon:stdio',
                        '-netdev', 'user,id=network0',
                        '-device', 'virtio-net-pci,netdev=network0,mac=52:54:00:12:34:56',
                        '-drive', 'file=%s,if=pflash,format=raw,unit=0,readonly=on' % self.uefi_code_path,
                        '-drive', 'file=%s.VARS.fd,if=pflash,format=raw,unit=1' % self.path,
                        '-drive', 'file=%s,if=none,id=harddrive0,format=qcow2' % self.path,
                        '-device', 'virtio-blk-pci,drive=harddrive0,bootindex=0',
                        '-drive', 'file=%s/cloud-init.seed,if=virtio,readonly=on' % self.autopkgtest_dir.name])

    def ready(self):
        """Returns true if the VM is booted and ready at userland"""
        # check captured serial for our marker
        self._mount()
        result = os.path.exists(os.path.join(self.autopkgtest_dir.name, 'img', 'TESTOK'))
        self._unmount()
        return result


