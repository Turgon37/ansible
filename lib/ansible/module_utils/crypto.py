# -*- coding: utf-8 -*-
#
# (c) 2016, Yanis Guenane <yanis+ansible@guenane.org>
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#
# --------------------------------------------------------------
# A clearly marked portion of this file is licensed under the BSD
# Copyright (c) 2015, 2016 Paul Kehrer (@reaperhulk)
# Copyright (c) 2017 Fraser Tweedale (@frasertweedale)
# For more details, search for the function _obj2txt().


try:
    import OpenSSL
    from OpenSSL import crypto
except ImportError:
    # An error will be raised in the calling class to let the end
    # user know that OpenSSL couldn't be found.
    pass

try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend as cryptography_backend
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives import hashes
    import ipaddress
except ImportError:
    # Error handled in the calling module.
    pass


import abc
import base64
import binascii
import datetime
import errno
import hashlib
import os
import re
import tempfile

from ansible.module_utils import six
from ansible.module_utils._text import to_bytes, to_text


class OpenSSLObjectError(Exception):
    pass


class OpenSSLBadPassphraseError(OpenSSLObjectError):
    pass


def get_fingerprint_of_bytes(source):
    """Generate the fingerprint of the given bytes."""

    fingerprint = {}

    try:
        algorithms = hashlib.algorithms
    except AttributeError:
        try:
            algorithms = hashlib.algorithms_guaranteed
        except AttributeError:
            return None

    for algo in algorithms:
        f = getattr(hashlib, algo)
        h = f(source)
        try:
            # Certain hash functions have a hexdigest() which expects a length parameter
            pubkey_digest = h.hexdigest()
        except TypeError:
            pubkey_digest = h.hexdigest(32)
        fingerprint[algo] = ':'.join(pubkey_digest[i:i + 2] for i in range(0, len(pubkey_digest), 2))

    return fingerprint


def get_fingerprint(path, passphrase=None):
    """Generate the fingerprint of the public key. """

    privatekey = load_privatekey(path, passphrase, check_passphrase=False)
    try:
        publickey = crypto.dump_publickey(crypto.FILETYPE_ASN1, privatekey)
    except AttributeError:
        # If PyOpenSSL < 16.0 crypto.dump_publickey() will fail.
        try:
            bio = crypto._new_mem_buf()
            rc = crypto._lib.i2d_PUBKEY_bio(bio, privatekey._pkey)
            if rc != 1:
                crypto._raise_current_error()
            publickey = crypto._bio_to_string(bio)
        except AttributeError:
            # By doing this we prevent the code from raising an error
            # yet we return no value in the fingerprint hash.
            return None
    return get_fingerprint_of_bytes(publickey)


def load_privatekey(path, passphrase=None, check_passphrase=True, content=None, backend='pyopenssl'):
    """Load the specified OpenSSL private key.

    The content can also be specified via content; in that case,
    this function will not load the key from disk.
    """

    try:
        if content is None:
            with open(path, 'rb') as b_priv_key_fh:
                priv_key_detail = b_priv_key_fh.read()
        else:
            priv_key_detail = content

        if backend == 'pyopenssl':

            # First try: try to load with real passphrase (resp. empty string)
            # Will work if this is the correct passphrase, or the key is not
            # password-protected.
            try:
                result = crypto.load_privatekey(crypto.FILETYPE_PEM,
                                                priv_key_detail,
                                                to_bytes(passphrase or ''))
            except crypto.Error as e:
                if len(e.args) > 0 and len(e.args[0]) > 0:
                    if e.args[0][0][2] in ('bad decrypt', 'bad password read'):
                        # This happens in case we have the wrong passphrase.
                        if passphrase is not None:
                            raise OpenSSLBadPassphraseError('Wrong passphrase provided for private key!')
                        else:
                            raise OpenSSLBadPassphraseError('No passphrase provided, but private key is password-protected!')
                raise OpenSSLObjectError('Error while deserializing key: {0}'.format(e))
            if check_passphrase:
                # Next we want to make sure that the key is actually protected by
                # a passphrase (in case we did try the empty string before, make
                # sure that the key is not protected by the empty string)
                try:
                    crypto.load_privatekey(crypto.FILETYPE_PEM,
                                           priv_key_detail,
                                           to_bytes('y' if passphrase == 'x' else 'x'))
                    if passphrase is not None:
                        # Since we can load the key without an exception, the
                        # key isn't password-protected
                        raise OpenSSLBadPassphraseError('Passphrase provided, but private key is not password-protected!')
                except crypto.Error as e:
                    if passphrase is None and len(e.args) > 0 and len(e.args[0]) > 0:
                        if e.args[0][0][2] in ('bad decrypt', 'bad password read'):
                            # The key is obviously protected by the empty string.
                            # Don't do this at home (if it's possible at all)...
                            raise OpenSSLBadPassphraseError('No passphrase provided, but private key is password-protected!')
        elif backend == 'cryptography':
            try:
                result = load_pem_private_key(priv_key_detail,
                                              passphrase,
                                              cryptography_backend())
            except TypeError as dummy:
                raise OpenSSLBadPassphraseError('Wrong or empty passphrase provided for private key')
            except ValueError as dummy:
                raise OpenSSLBadPassphraseError('Wrong passphrase provided for private key')

        return result
    except (IOError, OSError) as exc:
        raise OpenSSLObjectError(exc)


def load_certificate(path, backend='pyopenssl'):
    """Load the specified certificate."""

    try:
        with open(path, 'rb') as cert_fh:
            cert_content = cert_fh.read()
        if backend == 'pyopenssl':
            return crypto.load_certificate(crypto.FILETYPE_PEM, cert_content)
        elif backend == 'cryptography':
            return x509.load_pem_x509_certificate(cert_content, cryptography_backend())
    except (IOError, OSError) as exc:
        raise OpenSSLObjectError(exc)


def load_certificate_request(path, backend='pyopenssl'):
    """Load the specified certificate signing request."""
    try:
        with open(path, 'rb') as csr_fh:
            csr_content = csr_fh.read()
    except (IOError, OSError) as exc:
        raise OpenSSLObjectError(exc)
    if backend == 'pyopenssl':
        return crypto.load_certificate_request(crypto.FILETYPE_PEM, csr_content)
    elif backend == 'cryptography':
        return x509.load_pem_x509_csr(csr_content, cryptography_backend())


def parse_name_field(input_dict):
    """Take a dict with key: value or key: list_of_values mappings and return a list of tuples"""

    result = []
    for key in input_dict:
        if isinstance(input_dict[key], list):
            for entry in input_dict[key]:
                result.append((key, entry))
        else:
            result.append((key, input_dict[key]))
    return result


def convert_relative_to_datetime(relative_time_string):
    """Get a datetime.datetime or None from a string in the time format described in sshd_config(5)"""

    parsed_result = re.match(
        r"^(?P<prefix>[+-])((?P<weeks>\d+)[wW])?((?P<days>\d+)[dD])?((?P<hours>\d+)[hH])?((?P<minutes>\d+)[mM])?((?P<seconds>\d+)[sS]?)?$",
        relative_time_string)

    if parsed_result is None or len(relative_time_string) == 1:
        # not matched or only a single "+" or "-"
        return None

    offset = datetime.timedelta(0)
    if parsed_result.group("weeks") is not None:
        offset += datetime.timedelta(weeks=int(parsed_result.group("weeks")))
    if parsed_result.group("days") is not None:
        offset += datetime.timedelta(days=int(parsed_result.group("days")))
    if parsed_result.group("hours") is not None:
        offset += datetime.timedelta(hours=int(parsed_result.group("hours")))
    if parsed_result.group("minutes") is not None:
        offset += datetime.timedelta(
            minutes=int(parsed_result.group("minutes")))
    if parsed_result.group("seconds") is not None:
        offset += datetime.timedelta(
            seconds=int(parsed_result.group("seconds")))

    if parsed_result.group("prefix") == "+":
        return datetime.datetime.utcnow() + offset
    else:
        return datetime.datetime.utcnow() - offset


def select_message_digest(digest_string):
    digest = None
    if digest_string == 'sha256':
        digest = hashes.SHA256()
    elif digest_string == 'sha384':
        digest = hashes.SHA384()
    elif digest_string == 'sha512':
        digest = hashes.SHA512()
    elif digest_string == 'sha1':
        digest = hashes.SHA1()
    elif digest_string == 'md5':
        digest = hashes.MD5()
    return digest


def write_file(module, content, default_mode=None):
    '''
    Writes content into destination file as securely as possible.
    Uses file arguments from module.
    '''
    # Find out parameters for file
    file_args = module.load_file_common_arguments(module.params)
    if file_args['mode'] is None:
        file_args['mode'] = default_mode
    # Create tempfile name
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=b'.ansible_tmp')
    try:
        os.close(tmp_fd)
    except Exception as dummy:
        pass
    module.add_cleanup_file(tmp_name)  # if we fail, let Ansible try to remove the file
    try:
        try:
            # Create tempfile
            file = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(file, content)
            os.close(file)
        except Exception as e:
            try:
                os.remove(tmp_name)
            except Exception as dummy:
                pass
            module.fail_json(msg='Error while writing result into temporary file: {0}'.format(e))
        # Update destination to wanted permissions
        if os.path.exists(file_args['path']):
            module.set_fs_attributes_if_different(file_args, False)
        # Move tempfile to final destination
        module.atomic_move(tmp_name, file_args['path'])
        # Try to update permissions again
        module.set_fs_attributes_if_different(file_args, False)
    except Exception as e:
        try:
            os.remove(tmp_name)
        except Exception as dummy:
            pass
        module.fail_json(msg='Error while writing result: {0}'.format(e))


@six.add_metaclass(abc.ABCMeta)
class OpenSSLObject(object):

    def __init__(self, path, state, force, check_mode):
        self.path = path
        self.state = state
        self.force = force
        self.name = os.path.basename(path)
        self.changed = False
        self.check_mode = check_mode

    def check(self, module, perms_required=True):
        """Ensure the resource is in its desired state."""

        def _check_state():
            return os.path.exists(self.path)

        def _check_perms(module):
            file_args = module.load_file_common_arguments(module.params)
            return not module.set_fs_attributes_if_different(file_args, False)

        if not perms_required:
            return _check_state()

        return _check_state() and _check_perms(module)

    @abc.abstractmethod
    def dump(self):
        """Serialize the object into a dictionary."""

        pass

    @abc.abstractmethod
    def generate(self):
        """Generate the resource."""

        pass

    def remove(self, module):
        """Remove the resource from the filesystem."""

        try:
            os.remove(self.path)
            self.changed = True
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise OpenSSLObjectError(exc)
            else:
                pass


_OID_MAP = {
    # First entry is 'canonical' name
    "2.5.29.37.0": ('Any Extended Key Usage', 'anyExtendedKeyUsage'),
    "1.3.6.1.5.5.7.1.3": ('qcStatements', ),
    "1.3.6.1.5.5.7.3.10": ('DVCS', 'dvcs'),
    "1.3.6.1.5.5.7.3.7": ('IPSec User', 'ipsecUser'),
    "1.3.6.1.5.5.7.1.2": ('Biometric Info', 'biometricInfo'),
}

_NORMALIZE_NAMES = {
    'CN': 'commonName',
    'C': 'countryName',
    'L': 'localityName',
    'ST': 'stateOrProvinceName',
    'street': 'streetAddress',
    'O': 'organizationName',
    'OU': 'organizationalUnitName',
    'SN': 'surname',
    'GN': 'givenName',
    'UID': 'userId',
    'userID': 'userId',
    'DC': 'domainComponent',
    'jurisdictionC': 'jurisdictionCountryName',
    'jurisdictionL': 'jurisdictionLocalityName',
    'jurisdictionST': 'jurisdictionStateOrProvinceName',
    'serverAuth': 'TLS Web Server Authentication',
    'clientAuth': 'TLS Web Client Authentication',
    'codeSigning': 'Code Signing',
    'emailProtection': 'E-mail Protection',
    'timeStamping': 'Time Stamping',
    'OCSPSigning': 'OCSP Signing',
}

for dotted, names in _OID_MAP.items():
    for name in names[1:]:
        _NORMALIZE_NAMES[name] = names[0]


def pyopenssl_normalize_name(name):
    nid = OpenSSL._util.lib.OBJ_txt2nid(to_bytes(name))
    if nid != 0:
        b_name = OpenSSL._util.lib.OBJ_nid2ln(nid)
        name = to_text(OpenSSL._util.ffi.string(b_name))
    return _NORMALIZE_NAMES.get(name, name)


# #####################################################################################
# #####################################################################################
# # This excerpt is dual licensed under the terms of the Apache License, Version
# # 2.0, and the BSD License. See the LICENSE file at
# # https://github.com/pyca/cryptography/blob/master/LICENSE for complete details.
# #
# # Adapted from cryptography's hazmat/backends/openssl/decode_asn1.py
# #
# # Copyright (c) 2015, 2016 Paul Kehrer (@reaperhulk)
# # Copyright (c) 2017 Fraser Tweedale (@frasertweedale)
# #
# # Relevant commits from cryptography project (https://github.com/pyca/cryptography):
# #    pyca/cryptography@719d536dd691e84e208534798f2eb4f82aaa2e07
# #    pyca/cryptography@5ab6d6a5c05572bd1c75f05baf264a2d0001894a
# #    pyca/cryptography@2e776e20eb60378e0af9b7439000d0e80da7c7e3
# #    pyca/cryptography@fb309ed24647d1be9e319b61b1f2aa8ebb87b90b
# #    pyca/cryptography@2917e460993c475c72d7146c50dc3bbc2414280d
# #    pyca/cryptography@3057f91ea9a05fb593825006d87a391286a4d828
# #    pyca/cryptography@d607dd7e5bc5c08854ec0c9baff70ba4a35be36f
def _obj2txt(openssl_lib, openssl_ffi, obj):
    # Set to 80 on the recommendation of
    # https://www.openssl.org/docs/crypto/OBJ_nid2ln.html#return_values
    #
    # But OIDs longer than this occur in real life (e.g. Active
    # Directory makes some very long OIDs).  So we need to detect
    # and properly handle the case where the default buffer is not
    # big enough.
    #
    buf_len = 80
    buf = openssl_ffi.new("char[]", buf_len)

    # 'res' is the number of bytes that *would* be written if the
    # buffer is large enough.  If 'res' > buf_len - 1, we need to
    # alloc a big-enough buffer and go again.
    res = openssl_lib.OBJ_obj2txt(buf, buf_len, obj, 1)
    if res > buf_len - 1:  # account for terminating null byte
        buf_len = res + 1
        buf = openssl_ffi.new("char[]", buf_len)
        res = openssl_lib.OBJ_obj2txt(buf, buf_len, obj, 1)
    return openssl_ffi.buffer(buf, res)[:].decode()
# #####################################################################################
# #####################################################################################


def cryptography_get_extensions_from_cert(cert):
    # Since cryptography won't give us the DER value for an extension
    # (that is only stored for unrecognized extensions), we have to re-do
    # the extension parsing outselves.
    result = dict()
    backend = cert._backend
    x509_obj = cert._x509

    for i in range(backend._lib.X509_get_ext_count(x509_obj)):
        ext = backend._lib.X509_get_ext(x509_obj, i)
        if ext == backend._ffi.NULL:
            continue
        crit = backend._lib.X509_EXTENSION_get_critical(ext)
        data = backend._lib.X509_EXTENSION_get_data(ext)
        backend.openssl_assert(data != backend._ffi.NULL)
        der = backend._ffi.buffer(data.data, data.length)[:]
        entry = dict(
            critical=(crit == 1),
            value=base64.b64encode(der),
        )
        oid = _obj2txt(backend._lib, backend._ffi, backend._lib.X509_EXTENSION_get_object(ext))
        result[oid] = entry
    return result


def pyopenssl_get_extensions_from_cert(cert):
    # While pyOpenSSL allows us to get an extension's DER value, it won't
    # give us the dotted string for an OID. So we have to do some magic to
    # get hold of it.
    result = dict()
    ext_count = cert.get_extension_count()
    for i in range(0, ext_count):
        ext = cert.get_extension(i)
        entry = dict(
            critical=bool(ext.get_critical()),
            value=base64.b64encode(ext.get_data()),
        )
        oid = _obj2txt(
            OpenSSL._util.lib,
            OpenSSL._util.ffi,
            OpenSSL._util.lib.X509_EXTENSION_get_object(ext._extension)
        )
        # This could also be done a bit simpler:
        #
        #   oid = _obj2txt(OpenSSL._util.lib, OpenSSL._util.ffi, OpenSSL._util.lib.OBJ_nid2obj(ext._nid))
        #
        # Unfortunately this gives the wrong result in case the linked OpenSSL
        # doesn't know the OID. That's why we have to get the OID dotted string
        # similarly to how cryptography does it.
        result[oid] = entry
    return result


def crpytography_name_to_oid(name):
    if name in ('CN', 'commonName'):
        return x509.oid.NameOID.COMMON_NAME
    if name in ('C', 'countryName'):
        return x509.oid.NameOID.COUNTRY_NAME
    if name in ('L', 'localityName'):
        return x509.oid.NameOID.LOCALITY_NAME
    if name in ('ST', 'stateOrProvinceName'):
        return x509.oid.NameOID.STATE_OR_PROVINCE_NAME
    if name in ('street', 'streetAddress'):
        return x509.oid.NameOID.STREET_ADDRESS
    if name in ('O', 'organizationName'):
        return x509.oid.NameOID.ORGANIZATION_NAME
    if name in ('OU', 'organizationalUnitName'):
        return x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME
    if name in ('serialNumber', ):
        return x509.oid.NameOID.SERIAL_NUMBER
    if name in ('SN', 'surname'):
        return x509.oid.NameOID.SURNAME
    if name in ('GN', 'givenName'):
        return x509.oid.NameOID.GIVEN_NAME
    if name in ('title', ):
        return x509.oid.NameOID.TITLE
    if name in ('generationQualifier', ):
        return x509.oid.NameOID.GENERATION_QUALIFIER
    if name in ('x500UniqueIdentifier', ):
        return x509.oid.NameOID.X500_UNIQUE_IDENTIFIER
    if name in ('dnQualifier', ):
        return x509.oid.NameOID.DN_QUALIFIER
    if name in ('pseudonym', ):
        return x509.oid.NameOID.PSEUDONYM
    if name in ('UID', 'userId', 'UserID'):
        return x509.oid.NameOID.USER_ID
    if name in ('DC', 'domainComponent'):
        return x509.oid.NameOID.DOMAIN_COMPONENT
    if name in ('emailAddress', ):
        return x509.oid.NameOID.EMAIL_ADDRESS
    if name in ('jurisdictionC', 'jurisdictionCountryName'):
        return x509.oid.NameOID.JURISDICTION_COUNTRY_NAME
    if name in ('jurisdictionL', 'jurisdictionLocalityName'):
        return x509.oid.NameOID.JURISDICTION_LOCALITY_NAME
    if name in ('jurisdictionST', 'jurisdictionStateOrProvinceName'):
        return x509.oid.NameOID.JURISDICTION_STATE_OR_PROVINCE_NAME
    if name in ('businessCategory', ):
        return x509.oid.NameOID.BUSINESS_CATEGORY
    if name in ('postalAddress', ):
        return x509.oid.NameOID.POSTAL_ADDRESS
    if name in ('postalCode', ):
        return x509.oid.NameOID.POSTAL_CODE
    if name in ('serverAuth', 'TLS Web Server Authentication'):
        return x509.oid.ExtendedKeyUsageOID.SERVER_AUTH
    if name in ('clientAuth', 'TLS Web Client Authentication'):
        return x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH
    if name in ('codeSigning', 'Code Signing'):
        return x509.oid.ExtendedKeyUsageOID.CODE_SIGNING
    if name in ('emailProtection', 'E-mail Protection'):
        return x509.oid.ExtendedKeyUsageOID.EMAIL_PROTECTION
    if name in ('timeStamping', 'Time Stamping'):
        return x509.oid.ExtendedKeyUsageOID.TIME_STAMPING
    if name in ('OCSPSigning', 'OCSP Signing'):
        return x509.oid.ExtendedKeyUsageOID.OCSP_SIGNING
    if name in ('anyExtendedKeyUsage', 'Any Extended Key Usage'):
        return x509.oid.ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE
    for dotted, names in _OID_MAP.items():
        if name in names:
            return x509.oid.ObjectIdentifier(dotted)
    raise OpenSSLObjectError('Cannot find OID for "{0}"'.format(name))


def crpytography_oid_to_name(oid):
    dotted_string = oid.dotted_string
    names = _OID_MAP.get(dotted_string)
    name = names[0] if names else oid._name
    return _NORMALIZE_NAMES.get(name, name)


def cryptography_get_name_oid(id):
    '''
    Given a symbolic ID, finds the appropriate OID for use with cryptography.
    Raises an OpenSSLObjectError if the ID is unknown.
    '''
    if id in ('CN', 'commonName'):
        return x509.oid.NameOID.COMMON_NAME
    if id in ('C', 'countryName'):
        return x509.oid.NameOID.COUNTRY_NAME
    if id in ('L', 'localityName'):
        return x509.oid.NameOID.LOCALITY_NAME
    if id in ('ST', 'stateOrProvinceName'):
        return x509.oid.NameOID.STATE_OR_PROVINCE_NAME
    if id in ('street', 'streetAddress'):
        return x509.oid.NameOID.STREET_ADDRESS
    if id in ('O', 'organizationName'):
        return x509.oid.NameOID.ORGANIZATION_NAME
    if id in ('OU', 'organizationalUnitName'):
        return x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME
    if id in ('serialNumber', ):
        return x509.oid.NameOID.SERIAL_NUMBER
    if id in ('SN', 'surname'):
        return x509.oid.NameOID.SURNAME
    if id in ('GN', 'givenName'):
        return x509.oid.NameOID.GIVEN_NAME
    if id in ('title', ):
        return x509.oid.NameOID.TITLE
    if id in ('generationQualifier', ):
        return x509.oid.NameOID.GENERATION_QUALIFIER
    if id in ('x500UniqueIdentifier', ):
        return x509.oid.NameOID.X500_UNIQUE_IDENTIFIER
    if id in ('dnQualifier', ):
        return x509.oid.NameOID.DN_QUALIFIER
    if id in ('pseudonym', ):
        return x509.oid.NameOID.PSEUDONYM
    if id in ('UID', 'userId', 'userID'):
        return x509.oid.NameOID.USER_ID
    if id in ('DC', 'domainComponent'):
        return x509.oid.NameOID.DOMAIN_COMPONENT
    if id in ('emailAddress', ):
        return x509.oid.NameOID.EMAIL_ADDRESS
    if id in ('jurisdictionC', 'jurisdictionCountryName'):
        return x509.oid.NameOID.JURISDICTION_COUNTRY_NAME
    if id in ('jurisdictionL', 'jurisdictionLocalityName'):
        return x509.oid.NameOID.JURISDICTION_LOCALITY_NAME
    if id in ('jurisdictionST', 'jurisdictionStateOrProvinceName'):
        return x509.oid.NameOID.JURISDICTION_STATE_OR_PROVINCE_NAME
    if id in ('businessCategory', ):
        return x509.oid.NameOID.BUSINESS_CATEGORY
    if id in ('postalAddress', ):
        return x509.oid.NameOID.POSTAL_ADDRESS
    if id in ('postalCode', ):
        return x509.oid.NameOID.POSTAL_CODE
    raise OpenSSLObjectError('Unknown subject field identifier "{0}"'.format(id))


def cryptography_get_name(name):
    '''
    Given a name string, returns a cryptography x509.Name object.
    Raises an OpenSSLObjectError if the name is unknown or cannot be parsed.
    '''
    try:
        if name.startswith('DNS:'):
            return x509.DNSName(to_text(name[4:]))
        if name.startswith('IP:'):
            return x509.IPAddress(ipaddress.ip_address(to_text(name[3:])))
        if name.startswith('email:'):
            return x509.RFC822Name(to_text(name[6:]))
        if name.startswith('URI:'):
            return x509.UniformResourceIdentifier(to_text(name[4:]))
    except Exception as e:
        raise OpenSSLObjectError('Cannot parse Subject Alternative Name "{0}": {1}'.format(name, e))
    if ':' not in name:
        raise OpenSSLObjectError('Cannot parse Subject Alternative Name "{0}" (forgot "DNS:" prefix?)'.format(name))
    raise OpenSSLObjectError('Cannot parse Subject Alternative Name "{0}" (potentially unsupported by cryptography backend)'.format(name))


def _get_hex(bytes):
    if bytes is None:
        return bytes
    data = binascii.hexlify(bytes)
    data = to_text(b':'.join(data[i:i + 2] for i in range(0, len(data), 2)))
    return data


def cryptography_decode_name(name):
    '''
    Given a cryptography x509.Name object, returns a string.
    Raises an OpenSSLObjectError if the name is not supported.
    '''
    if isinstance(name, x509.DNSName):
        return 'DNS:{0}'.format(name.value)
    if isinstance(name, x509.IPAddress):
        return 'IP:{0}'.format(name.value.compressed)
    if isinstance(name, x509.RFC822Name):
        return 'email:{0}'.format(name.value)
    if isinstance(name, x509.UniformResourceIdentifier):
        return 'URI:{0}'.format(name.value)
    if isinstance(name, x509.DirectoryName):
        # FIXME: test
        return 'DirName:' + ''.join(['/{0}:{1}'.format(attribute.oid._name, attribute.value) for attribute in name.value])
    if isinstance(name, x509.RegisteredID):
        # FIXME: test
        return 'RegisteredID:{0}'.format(name.value)
    if isinstance(name, x509.OtherName):
        # FIXME: test
        return '{0}:{1}'.format(name.type_id.dotted_string, _get_hex(name.value))
    raise OpenSSLObjectError('Cannot decode name "{0}"'.format(name))


def _cryptography_get_keyusage(usage):
    '''
    Given a key usage identifier string, returns the parameter name used by cryptography's x509.KeyUsage().
    Raises an OpenSSLObjectError if the identifier is unknown.
    '''
    if usage in ('Digital Signature', 'digitalSignature'):
        return 'digital_signature'
    if usage in ('Non Repudiation', 'nonRepudiation'):
        return 'content_commitment'
    if usage in ('Key Encipherment', 'keyEncipherment'):
        return 'key_encipherment'
    if usage in ('Data Encipherment', 'dataEncipherment'):
        return 'data_encipherment'
    if usage in ('Key Agreement', 'keyAgreement'):
        return 'key_agreement'
    if usage in ('Certificate Sign', 'keyCertSign'):
        return 'key_cert_sign'
    if usage in ('CRL Sign', 'cRLSign'):
        return 'crl_sign'
    if usage in ('Encipher Only', 'encipherOnly'):
        return 'encipher_only'
    if usage in ('Decipher Only', 'decipherOnly'):
        return 'decipher_only'
    raise OpenSSLObjectError('Unknown key usage "{0}"'.format(usage))


def cryptography_parse_key_usage_params(usages):
    '''
    Given a list of key usage identifier strings, returns the parameters for cryptography's x509.KeyUsage().
    Raises an OpenSSLObjectError if an identifier is unknown.
    '''
    params = dict(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    for usage in usages:
        params[_cryptography_get_keyusage(usage)] = True
    return params


def cryptography_get_ext_keyusage(usage):
    '''
    Given an extended key usage identifier string, returns the OID used by cryptography.
    Raises an OpenSSLObjectError if the identifier is unknown.
    '''
    if usage in ('serverAuth', 'TLS Web Server Authentication'):
        return x509.oid.ExtendedKeyUsageOID.SERVER_AUTH
    if usage in ('clientAuth', 'TLS Web Client Authentication'):
        return x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH
    if usage in ('codeSigning', 'Code Signing'):
        return x509.oid.ExtendedKeyUsageOID.CODE_SIGNING
    if usage in ('emailProtection', 'E-mail Protection'):
        return x509.oid.ExtendedKeyUsageOID.EMAIL_PROTECTION
    if usage in ('timeStamping', 'Time Stamping'):
        return x509.oid.ExtendedKeyUsageOID.TIME_STAMPING
    if usage in ('OCSPSigning', 'OCSP Signing'):
        return x509.oid.ExtendedKeyUsageOID.OCSP_SIGNING
    if usage in ('anyExtendedKeyUsage', 'Any Extended Key Usage'):
        return x509.oid.ObjectIdentifier("2.5.29.37.0")
    if usage in ('qcStatements', ):
        return x509.oid.ObjectIdentifier("1.3.6.1.5.5.7.1.3")
    if usage in ('DVCS', 'dvcs'):
        return x509.oid.ObjectIdentifier("1.3.6.1.5.5.7.3.10")
    if usage in ('IPSec User', 'ipsecUser'):
        return x509.oid.ObjectIdentifier("1.3.6.1.5.5.7.3.7")
    if usage in ('Biometric Info', 'biometricInfo'):
        return x509.oid.ObjectIdentifier("1.3.6.1.5.5.7.1.2")
    # FIXME need some more, probably all from https://www.iana.org/assignments/smi-numbers/smi-numbers.xhtml#smi-numbers-1.3.6.1.5.5.7.3
    raise OpenSSLObjectError('Unknown extended key usage "{0}"'.format(usage))


def cryptography_get_basic_constraints(constraints):
    '''
    Given a list of constraints, returns a tuple (ca, path_length).
    Raises an OpenSSLObjectError if a constraint is unknown or cannot be parsed.
    '''
    ca = False
    path_length = None
    if constraints:
        for constraint in constraints:
            if constraint.startswith('CA:'):
                if constraint == 'CA:TRUE':
                    ca = True
                elif constraint == 'CA:FALSE':
                    ca = False
                else:
                    raise OpenSSLObjectError('Unknown basic constraint value "{0}" for CA'.format(constraint[3:]))
            elif constraint.startswith('pathlen:'):
                v = constraint[len('pathlen:'):]
                try:
                    path_length = int(v)
                except Exception as e:
                    raise OpenSSLObjectError('Cannot parse path length constraint "{0}" ({1})'.format(v, e))
            else:
                raise OpenSSLObjectError('Unknown basic constraint "{0}"'.format(constraint))
    return ca, path_length


def binary_exp_mod(f, e, m):
    '''Computes f^e mod m in O(log e) multiplications modulo m.'''
    # Compute len_e = floor(log_2(e))
    len_e = -1
    x = e
    while x > 0:
        x >>= 1
        len_e += 1
    # Compute f**e mod m
    result = 1
    for k in range(len_e, -1, -1):
        result = (result * result) % m
        if ((e >> k) & 1) != 0:
            result = (result * f) % m
    return result


def simple_gcd(a, b):
    '''Compute GCD of its two inputs.'''
    while b != 0:
        a, b = b, a % b
    return a


def quick_is_not_prime(n):
    '''Does some quick checks to see if we can poke a hole into the primality of n.

    A result of `False` does **not** mean that the number is prime; it just means
    that we couldn't detect quickly whether it is not prime.
    '''
    if n <= 2:
        return True
    # The constant in the next line is the product of all primes < 200
    if simple_gcd(n, 7799922041683461553249199106329813876687996789903550945093032474868511536164700810) > 1:
        return True
    # TODO: maybe do some iterations of Miller-Rabin to increase confidence
    # (https://en.wikipedia.org/wiki/Miller%E2%80%93Rabin_primality_test)
    return False
