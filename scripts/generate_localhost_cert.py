from __future__ import annotations

import argparse
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def build_certificate(common_name: str, dns_names: list[str], output_dir: Path, days: int) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    key_path = output_dir / f"{common_name}.key"
    cert_path = output_dir / f"{common_name}.crt"

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    san_values: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    san_values.extend(
        [
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]
    )

    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san_values), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a localhost TLS certificate for local Docker HTTPS.")
    parser.add_argument("--output-dir", default="local-certs", help="Directory for localhost.crt and localhost.key")
    parser.add_argument("--common-name", default="localhost", help="Certificate common name")
    parser.add_argument("--days", type=int, default=365, help="Certificate validity in days")
    parser.add_argument("--dns-name", action="append", dest="dns_names", default=None, help="Extra DNS SAN entry")
    args = parser.parse_args()

    dns_names = args.dns_names or [args.common_name]
    cert_path, key_path = build_certificate(args.common_name, dns_names, Path(args.output_dir), args.days)
    print(cert_path)
    print(key_path)


if __name__ == "__main__":
    main()
