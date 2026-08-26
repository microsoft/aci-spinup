from __future__ import annotations


INSTALL_MODES = ("azure-linux-3", "ubuntu", "none")


def ssh_bootstrap_script(install_mode: str) -> str:
    if install_mode not in INSTALL_MODES:
        raise ValueError(f"unsupported install mode: {install_mode}")

    install = {
        "azure-linux-3": """\
if [ -f /etc/pki/rpm-gpg/MICROSOFT-RPM-GPG-KEY ]; then
    gpg --import /etc/pki/rpm-gpg/MICROSOFT-RPM-GPG-KEY
fi
tdnf update -y
tdnf install -y openssh-server ca-certificates""",
        "ubuntu": """\
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends openssh-server ca-certificates
rm -rf /var/lib/apt/lists/*""",
        "none": "",
    }[install_mode]

    sections = [
        "set -eu",
        """\
if [ "$(id -u)" -ne 0 ]; then
    echo 'aci-spinup SSH bootstrap requires the container image to run as root' >&2
    exit 1
fi""",
        """\
printf 'Fabric_NodeIPOrFQDN=%s\\n' "${Fabric_NodeIPOrFQDN:-}" >> /aci_env
printf 'UVM_SECURITY_CONTEXT_DIR=%s\\n' "${UVM_SECURITY_CONTEXT_DIR:-}" >> /aci_env""",
    ]
    if install:
        sections.append(install)
    sections.append(
        """\
mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh
printf '%s\\n' "$SSH_ADMIN_KEY" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A
cat >> /etc/ssh/sshd_config <<'ACI_SPINUP_SSH_CONFIG'
PermitRootLogin yes
PubkeyAuthentication yes
PasswordAuthentication no
ACI_SPINUP_SSH_CONFIG
exec /usr/sbin/sshd -D -e \
    -o PermitRootLogin=yes \
    -o PubkeyAuthentication=yes \
    -o PasswordAuthentication=no"""
    )
    return "\n".join(sections)


def ssh_bootstrap_command(install_mode: str) -> list[str]:
    return ["/bin/sh", "-c", ssh_bootstrap_script(install_mode)]
