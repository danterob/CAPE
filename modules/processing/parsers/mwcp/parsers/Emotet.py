# Copyright (C) 2017-2019 Kevin O'Reilly (kevin.oreilly@contextis.co.uk)
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from mwcp.parser import Parser
import struct, socket
import pefile
import yara
import os.path
import re
from Crypto.Util import asn1
from Crypto.PublicKey import RSA

rule_source = '''
rule Emotet
{
    meta:
        author = "kevoreilly"
        description = "Emotet Payload"
        cape_type = "Emotet Payload"
    strings:
        $snippet1 = {FF 15 ?? ?? ?? ?? 83 C4 0C 68 40 00 00 F0 6A 18}
        $snippet2 = {6A 13 68 01 00 01 00 FF 15 ?? ?? ?? ?? 85 C0}
        $snippet3 = {83 3D ?? ?? ?? ?? 00 C7 05 ?? ?? ?? ?? ?? ?? ?? ?? C7 05 ?? ?? ?? ?? ?? ?? ?? ?? 74 0A 51 E8 ?? ?? ?? ?? 83 C4 04 C3 33 C0 C3}
        $snippet4 = {33 C0 C7 05 ?? ?? ?? ?? ?? ?? ?? ?? C7 05 ?? ?? ?? ?? ?? ?? ?? ?? A3 ?? ?? ?? ?? A3 ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? 00 40 A3 ?? ?? ?? ?? 83 3C C5 ?? ?? ?? ?? 00 75 F0 51 E8 ?? ?? ?? ?? 83 C4 04 C3}
        $snippet5 = {8B E5 5D C3 B8 ?? ?? ?? ?? A3 ?? ?? ?? ?? A3 ?? ?? ?? ?? 33 C0 21 05 ?? ?? ?? ?? A3 ?? ?? ?? ?? 39 05 ?? ?? ?? ?? 74 18 40 A3 ?? ?? ?? ?? 83 3C C5 ?? ?? ?? ?? 00 75 F0 51 E8 ?? ?? ?? ?? 59 C3}
        $snippet6 = {33 C0 21 05 ?? ?? ?? ?? A3 ?? ?? ?? ?? 39 05 ?? ?? ?? ?? 74 18 40 A3 ?? ?? ?? ?? 83 3C C5 ?? ?? ?? ?? 00 75 F0 51 E8 ?? ?? ?? ?? 59 C3}
        $snippet7 = {8B 48 ?? C7 [5-6] C7 40 ?? ?? ?? ?? ?? C7 ?? ?? 00 00 00 [0-1] 83 3C CD ?? ?? ?? ?? 00 74 0E 41 89 48 ?? 83 3C CD ?? ?? ?? ?? 00 75 F2}
        $ref_rsa = {6A 00 6A 01 FF 76 ?? 8B 46 ?? FF D0 85 C0 74 ?? 8D 4D ?? E8 [4] 8D 45 ?? B9 [4] 8D 55 ?? 89 45 ?? E8}
    condition:
        //check for MZ Signature at offset 0
        uint16(0) == 0x5A4D and (($snippet1) and ($snippet2)) or ($snippet3) or ($snippet4) or ($snippet5) or ($snippet6) or ($snippet7) or ($ref_rsa)
}

'''

MAX_IP_STRING_SIZE = 16       # aaa.bbb.ccc.ddd\0

def yara_scan(raw_data, rule_name):
    addresses = {}
    yara_rules = yara.compile(source=rule_source)
    matches = yara_rules.match(data=raw_data)
    for match in matches:
        if match.rule == 'Emotet':
            for item in match.strings:
                if item[1] == rule_name:
                    addresses[item[1]] = item[0]
                    return addresses

def xor_data(data, key):
    l = len(key)
    decoded = ""
    for i in range(0, len(data)):
        decoded += chr(ord(data[i]) ^ ord(key[i % l]))
    return decoded

# This function is originally by Jason Reaves (@sysopfb),
# suggested as an addition by @pollo290987.
# A big thank you to both.
def extract_emotet_rsakey(filedata):
    pub_matches = re.findall('''\x30[\x00-\xff]{100}\x02\x03\x01\x00\x01\x00\x00''', filedata)
    if pub_matches:
        pub_key = pub_matches[0][0:106]
        seq = asn1.DerSequence()
        seq.decode(pub_key)
        return RSA.construct((seq[0], seq[1]))

class Emotet(Parser):
    def __init__(self, reporter=None):
        Parser.__init__(self, description='Emotet configuration parser.', author='kevoreilly', reporter=reporter)

    def run(self):
        filebuf = self.reporter.data
        pe = pefile.PE(data=self.reporter.data, fast_load=False)
        image_base = pe.OPTIONAL_HEADER.ImageBase

        pem_key = extract_emotet_rsakey(filebuf)
        if pem_key:
            self.reporter.add_metadata('other', {'RSA public key': pem_key.exportKey()})

        c2list = yara_scan(filebuf, '$c2list')
        if c2list:
            ips_offset = int(c2list['$c2list'])

            ip = struct.unpack('I', filebuf[ips_offset:ips_offset+4])[0]

            while ip:
                c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                port = str(struct.unpack('h', filebuf[ips_offset+4:ips_offset+6])[0])

                if c2_address and port:
                    self.reporter.add_metadata('address', c2_address+':'+port)

                ips_offset += 8
                ip = struct.unpack('I', filebuf[ips_offset:ips_offset+4])[0]
            return
        else:
            refc2list = yara_scan(filebuf, '$snippet3')
        if refc2list:
            c2list_va_offset = int(refc2list['$snippet3'])
            c2_list_va = struct.unpack('i', filebuf[c2list_va_offset+2:c2list_va_offset+6])[0]
            if c2_list_va - image_base > 0x20000:
                c2_list_va = c2_list_va & 0xffff
            else:
                c2_list_rva = c2_list_va - image_base
            try:
                c2_list_offset = pe.get_offset_from_rva(c2_list_rva)
            except PEFormatError as err:
                pass

            while 1:
                try:
                    ip = struct.unpack('<I', filebuf[c2_list_offset:c2_list_offset+4])[0]
                except:
                    break
                if ip == 0:
                    break
                c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                port = str(struct.unpack('H', filebuf[c2_list_offset+4:c2_list_offset+6])[0])

                if c2_address and port:
                    self.reporter.add_metadata('address', c2_address+':' + port)
                else:
                    break
                c2_list_offset += 8
        else:
            refc2list = yara_scan(filebuf, '$snippet4')
            if refc2list:
                c2list_va_offset = int(refc2list['$snippet4'])
                c2_list_va = struct.unpack('i', filebuf[c2list_va_offset+8:c2list_va_offset+12])[0]
                if c2_list_va - image_base > 0x20000:
                    c2_list_rva = c2_list_va & 0xffff
                else:
                    c2_list_rva = c2_list_va - image_base
                try:
                    c2_list_offset = pe.get_offset_from_rva(c2_list_rva)
                except PEFormatError as err:
                    pass

                while 1:
                    try:
                        ip = struct.unpack('<I', filebuf[c2_list_offset:c2_list_offset+4])[0]
                    except:
                        break
                    if ip == 0:
                        break
                    c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                    port = str(struct.unpack('H', filebuf[c2_list_offset+4:c2_list_offset+6])[0])

                    if c2_address and port:
                        self.reporter.add_metadata('address', c2_address+':' + port)
                    else:
                        break
                    c2_list_offset += 8
            else:
                refc2list = yara_scan(filebuf, '$snippet5')
                if refc2list:
                    c2list_va_offset = int(refc2list['$snippet5'])
                    c2_list_va = struct.unpack('i', filebuf[c2list_va_offset+5:c2list_va_offset+9])[0]
                    if c2_list_va - image_base > 0x20000:
                        c2_list_rva = c2_list_va & 0xffff
                    else:
                        c2_list_rva = c2_list_va - image_base
                    try:
                        c2_list_offset = pe.get_offset_from_rva(c2_list_rva)
                    except PEFormatError as err:
                        pass

                    while 1:
                        try:
                            ip = struct.unpack('<I', filebuf[c2_list_offset:c2_list_offset+4])[0]
                        except:
                            break
                        if ip == 0:
                            break
                        c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                        port = str(struct.unpack('H', filebuf[c2_list_offset+4:c2_list_offset+6])[0])

                        if c2_address and port:
                            self.reporter.add_metadata('address', c2_address+':' + port)
                        else:
                            break
                        c2_list_offset += 8
                else:
                    refc2list = yara_scan(filebuf, '$snippet6')
                    if refc2list:
                        c2list_va_offset = int(refc2list['$snippet6'])
                        c2_list_va = struct.unpack('i', filebuf[c2list_va_offset+15:c2list_va_offset+19])[0]
                        if c2_list_va - image_base > 0x20000:
                            c2_list_rva = c2_list_va & 0xffff
                        else:
                            c2_list_rva = c2_list_va - image_base
                        try:
                            c2_list_offset = pe.get_offset_from_rva(c2_list_rva)
                        except PEFormatError as err:
                            pass

                        while 1:
                            try:
                                ip = struct.unpack('<I', filebuf[c2_list_offset:c2_list_offset+4])[0]
                            except:
                                break
                            if ip == 0:
                                break
                            c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                            port = str(struct.unpack('H', filebuf[c2_list_offset+4:c2_list_offset+6])[0])

                            if c2_address and port:
                                self.reporter.add_metadata('address', c2_address+':' + port)
                            else:
                                break
                            c2_list_offset += 8
                    else:
                        refc2list = yara_scan(filebuf, '$snippet7')
                        if refc2list:
                            c2list_va_offset = int(refc2list['$snippet7'])
                            delta = 26
                            hb = struct.unpack('b', filebuf[c2list_va_offset+29:c2list_va_offset+30])[0]
                            if hb:
                                delta += 1
                            c2_list_va = struct.unpack('i', filebuf[c2list_va_offset+delta:c2list_va_offset+delta+4])[0]
                            if c2_list_va - image_base > 0x20000:
                                c2_list_rva = c2_list_va & 0xffff
                            else:
                                c2_list_rva = c2_list_va - image_base
                            try:
                                c2_list_offset = pe.get_offset_from_rva(c2_list_rva)
                            except PEFormatError as err:
                                pass

                            while 1:
                                try:
                                    ip = struct.unpack('<I', filebuf[c2_list_offset:c2_list_offset+4])[0]
                                except:
                                    break
                                if ip == 0:
                                    break
                                c2_address = socket.inet_ntoa(struct.pack('!L', ip))
                                port = str(struct.unpack('H', filebuf[c2_list_offset+4:c2_list_offset+6])[0])

                                if c2_address and port:
                                    self.reporter.add_metadata('address', c2_address+':' + port)
                                else:
                                    break
                                c2_list_offset += 8

        if not pem_key:
            ref_rsa = yara_scan(filebuf, '$ref_rsa')
            if ref_rsa:
                ref_rsa_offset = int(ref_rsa['$ref_rsa'])
                ref_rsa_va = struct.unpack('i', filebuf[ref_rsa_offset+28:ref_rsa_offset+32])[0]
                ref_rsa_rva = ref_rsa_va - image_base
                try:
                    ref_rsa_offset = pe.get_offset_from_rva(ref_rsa_rva)
                except:
                    return
                key = struct.unpack('<I', filebuf[ref_rsa_offset:ref_rsa_offset+4])[0]
                xorsize = key ^ struct.unpack('<I', filebuf[ref_rsa_offset+4:ref_rsa_offset+8])[0]
                rsa_key = xor_data(filebuf[ref_rsa_offset+8:ref_rsa_offset+8+xorsize], struct.pack('<I',key))
                seq = asn1.DerSequence()
                seq.decode(rsa_key)
                self.reporter.add_metadata('other', {'RSA public key': RSA.construct((seq[0], seq[1])).exportKey()})
