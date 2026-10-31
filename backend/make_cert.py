"""
Create the self-signed HTTPS certificate the phone-sensor bridge (tls_proxy.py) needs.

Run once from the backend folder:   py make_cert.py
Writes certs/cert.pem and certs/key.pem, valid for this laptop's current IP
addresses, the Windows hotspot address 192.168.137.1, 127.0.0.1 and localhost.
Re-run it if the laptop's IP changes. Needs:  pip install cryptography
"""
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def local_ipv4s():
    ips = {"127.0.0.1", "192.168.137.1"}          # loopback + Windows Mobile hotspot
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:                                           # address of the default route
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def main():
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    os.makedirs(base, exist_ok=True)
    ips = local_ipv4s()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BhuRakshak local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(os.path.join(base, "key.pem"), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(os.path.join(base, "cert.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print("certs/cert.pem + certs/key.pem written for:", ", ".join(ips))


if __name__ == "__main__":
    main()
