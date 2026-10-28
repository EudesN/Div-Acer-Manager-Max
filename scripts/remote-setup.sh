#!/bin/bash

# DAMX remote installer: downloads the latest DAMX release and installs it.
#
# Environment overrides:
#   LINUWU_SENSE_REPO  driver repo cloned only if the release does not bundle
#                      Linuwu-Sense/ (default: PXDiv/Div-Linuwu-Sense)
#   LINUWU_SENSE_REF   branch/tag/commit to use for that clone (default: the
#                      repository's default branch)

# Constants
SCRIPT_VERSION="0.7.10-1"
INSTALL_DIR="/opt/damx"
BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"
DAEMON_SERVICE_NAME="damx-daemon.service"
DESKTOP_FILE_DIR="/usr/share/applications"
ICON_DIR="/usr/share/icons/hicolor/256x256/apps"
LINUWU_SENSE_REPO="${LINUWU_SENSE_REPO:-PXDiv/Div-Linuwu-Sense}"
LINUWU_SENSE_REF="${LINUWU_SENSE_REF:-}"
DAMX_REPO="PXDiv/Div-Acer-Manager-Max"
MODULE_SIGNING_DIR="/var/lib/damx/secureboot"
MODULE_SIGNING_KEY="${MODULE_SIGNING_DIR}/DAMX-MOK.priv"
MODULE_SIGNING_CERT="${MODULE_SIGNING_DIR}/DAMX-MOK.der"
MODULE_SIGNING_REQUIRED=false
LINUWU_SENSE_DKMS_NAME="linuwu-sense"
DKMS_SIGNING_CONF="/etc/dkms/framework.conf.d/damx.conf"
WORKDIR=""
RELEASE_TARBALL=""
RELEASE_ROOT=""
DRIVER_SOURCE_DIR=""

# Legacy paths for cleanup (uppercase naming convention)
LEGACY_INSTALL_DIR="/opt/DAMX"
LEGACY_DAEMON_SERVICE_NAME="DAMX-Daemon.service"

# Paths used by the detection helpers (kept in variables so they can be
# exercised by a test harness).
KERNEL_MODULES_DIR="/lib/modules"
PROC_VERSION_FILE="/proc/version"

# Legacy driver permission config. The driver now ships a udev rule instead;
# a stale tmpfiles entry makes pacman's tmpfiles hook fail (issue #182).
LEGACY_TMPFILES_CONF="/etc/tmpfiles.d/linuwu_sense.conf"
LINUWU_SENSE_UDEV_RULE="/etc/udev/rules.d/99-linuwu-sense.rules"

# Group that may talk to the daemon socket (daemon makes it root:damx 0660).
DAMX_GROUP="damx"
DAMX_GROUP_ADDED=false
REAL_USER=""

# Extra make arguments for the kernel build (LLVM=1 CC=clang on clang kernels).
DRIVER_BUILD_ARGS=()

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to pause script execution
pause() {
  echo -e "${BLUE}Press any key to continue...${NC}"
  read -n 1 -s -r
}

# ---------------------------------------------------------------------------
# Kernel / toolchain detection. Defined before anything that calls it
# (GitHub issues #223/#256: "is_llvm_kernel: command not found").
# ---------------------------------------------------------------------------

# Succeeds when the running kernel was compiled with clang/LLVM, in which case
# out-of-tree modules must be built with LLVM=1 CC=clang.
is_llvm_kernel() {
  local kernel_release
  local kernel_build
  local config_file

  kernel_release=$(uname -r)
  kernel_build="${KERNEL_MODULES_DIR}/${kernel_release}/build"

  # The kernel config is authoritative when it is available.
  for config_file in "${kernel_build}/.config" "/boot/config-${kernel_release}"; do
    if [ -r "$config_file" ]; then
      grep -q '^CONFIG_CC_IS_CLANG=y' "$config_file"
      return $?
    fi
  done

  if command -v zgrep &> /dev/null && [ -r /proc/config.gz ]; then
    zgrep -q '^CONFIG_CC_IS_CLANG=y' /proc/config.gz
    return $?
  fi

  if [ -r "$PROC_VERSION_FILE" ] && grep -qi 'clang' "$PROC_VERSION_FILE"; then
    return 0
  fi

  if [ -r "${kernel_build}/include/generated/compile.h" ] &&
     grep -qsi 'clang' "${kernel_build}/include/generated/compile.h"; then
    return 0
  fi

  return 1
}

# Prints the package manager in use.
detect_pkg_manager() {
  local pm
  for pm in pacman apt-get dnf yum zypper; do
    if command -v "$pm" &> /dev/null; then
      echo "$pm"
      return 0
    fi
  done
  return 1
}

# Arch: print the headers package matching the running kernel, e.g.
# linux -> linux-headers, linux-zen -> linux-zen-headers.
arch_headers_pkg() {
  local kernel_pkg
  kernel_pkg=$(pacman -Qoq "/usr/lib/modules/$(uname -r)/vmlinuz" 2> /dev/null | head -n 1)
  if [ -z "$kernel_pkg" ]; then
    return 1
  fi
  echo "${kernel_pkg}-headers"
}

# Succeeds when <dir>/src/linuwu_sense.c defines DRIVER_VERSION, which only
# the Div-Linuwu-Sense fork (with the ANV15-51 fixes) does.
has_driver_version_define() {
  grep -qE '^[[:space:]]*#define[[:space:]]+DRIVER_VERSION[[:space:]]+"' "$1/src/linuwu_sense.c" 2> /dev/null
}

# Prints the DRIVER_VERSION of the driver source in <dir> (fallback "1.0").
get_driver_version() {
  local version
  version=$(sed -n -E 's/^[[:space:]]*#define[[:space:]]+DRIVER_VERSION[[:space:]]+"([^"]+)".*/\1/p' "$1/src/linuwu_sense.c" 2> /dev/null | head -n 1)
  echo "${version:-1.0}"
}

# Determine the unprivileged user that invoked the installer.
resolve_real_user() {
  local user="${SUDO_USER:-}"

  if [ -z "$user" ] || [ "$user" = "root" ]; then
    user=$(logname 2> /dev/null || true)
  fi
  if [ "$user" = "root" ] || { [ -n "$user" ] && ! id -u "$user" > /dev/null 2>&1; }; then
    user=""
  fi
  echo "$user"
}

print_banner() {
  clear
  echo -e "${BLUE}==========================================${NC}"
  echo -e "${BLUE}      DAMX Suite Installer v${SCRIPT_VERSION}        ${NC}"
  echo -e "${BLUE}   Acer Laptop WMI Controls for Linux     ${NC}"
  echo -e "${BLUE}==========================================${NC}"
  echo ""
}

secure_boot_enabled() {
  local secure_boot_state
  local secure_boot_value
  local secure_boot_var

  if [ -r /sys/module/module/parameters/sig_enforce ] &&
     grep -qiE '^(1|y|yes)$' /sys/module/module/parameters/sig_enforce; then
    return 0
  fi

  if command -v mokutil &> /dev/null; then
    secure_boot_state=$(mokutil --sb-state 2>/dev/null || true)
    if echo "$secure_boot_state" | grep -qi 'SecureBoot enabled'; then
      return 0
    fi
    if echo "$secure_boot_state" | grep -qi 'SecureBoot disabled'; then
      return 1
    fi
  fi

  for secure_boot_var in /sys/firmware/efi/efivars/SecureBoot-*; do
    [ -r "$secure_boot_var" ] || continue
    secure_boot_value=$(od -An -j4 -N1 -t u1 "$secure_boot_var" 2>/dev/null | tr -d ' ')
    if [ "$secure_boot_value" = "1" ]; then
      return 0
    fi
  done

  return 1
}

install_secure_boot_deps() {
  if command -v pacman &> /dev/null; then
    pacman -S --needed --noconfirm mokutil openssl
  elif command -v apt-get &> /dev/null; then
    apt-get update && apt-get install -y mokutil openssl
  elif command -v dnf &> /dev/null; then
    dnf install -y mokutil openssl
  elif command -v yum &> /dev/null; then
    yum install -y mokutil openssl
  elif command -v zypper &> /dev/null; then
    zypper install -y mokutil openssl
  else
    echo -e "${RED}Error: Install mokutil and openssl before continuing.${NC}"
    return 1
  fi
}

prepare_secure_boot_signing() {
  local key_check_output

  MODULE_SIGNING_REQUIRED=false
  if ! secure_boot_enabled; then
    return 0
  fi

  MODULE_SIGNING_REQUIRED=true
  echo -e "${YELLOW}Secure Boot is enabled; the Linuwu-Sense module must be signed.${NC}"

  if ! command -v mokutil &> /dev/null || ! command -v openssl &> /dev/null; then
    echo -e "${YELLOW}Installing Secure Boot signing tools...${NC}"
    install_secure_boot_deps || return 1
  fi

  install -d -m 700 "$MODULE_SIGNING_DIR" || return 1

  if [ ! -s "$MODULE_SIGNING_KEY" ] || [ ! -s "$MODULE_SIGNING_CERT" ]; then
    echo -e "${YELLOW}Generating a DAMX Machine Owner Key...${NC}"
    (
      umask 077
      openssl req -new -x509 -newkey rsa:4096 \
        -keyout "${MODULE_SIGNING_KEY}.new" \
        -addext "extendedKeyUsage=codeSigning" \
        -outform DER \
        -out "${MODULE_SIGNING_CERT}.new" \
        -nodes \
        -days 36500 \
        -subj "/CN=DAMX Linuwu-Sense Module Signing/"
    ) || return 1
    mv -f "${MODULE_SIGNING_KEY}.new" "$MODULE_SIGNING_KEY"
    mv -f "${MODULE_SIGNING_CERT}.new" "$MODULE_SIGNING_CERT"
    chmod 600 "$MODULE_SIGNING_KEY"
    chmod 644 "$MODULE_SIGNING_CERT"
  fi

  key_check_output=$(LC_ALL=C mokutil --test-key "$MODULE_SIGNING_CERT" 2>&1 || true)
  case "$key_check_output" in
    *"is already enrolled"*|*"is already in db"*|*"built-in trusted keyring"*)
      echo -e "${GREEN}DAMX module-signing key is enrolled.${NC}"
      return 0
      ;;
    *"already in the enrollment request"*)
      echo -e "${YELLOW}DAMX MOK enrollment is already pending.${NC}"
      echo -e "${YELLOW}Reboot, complete enrollment in MOK Manager, then run this installer again.${NC}"
      return 2
      ;;
    *"blocked"*)
      echo -e "${RED}Error: The DAMX module-signing key is blocked by Secure Boot policy.${NC}"
      return 1
      ;;
    *"is not enrolled"*)
      ;;
    *)
      echo -e "${RED}Error: Could not determine whether the DAMX signing key is enrolled.${NC}"
      echo "$key_check_output"
      return 1
      ;;
  esac

  if [ ! -t 0 ]; then
    echo -e "${RED}Error: MOK enrollment requires an interactive terminal.${NC}"
    echo "Run this installer directly from a terminal, then reboot and enroll the DAMX key."
    return 1
  fi

  echo -e "${YELLOW}The DAMX signing key must be enrolled before the driver can load.${NC}"
  echo "You will be asked for a one-time password. After rebooting, choose:"
  echo "  Enroll MOK -> Continue -> Yes"
  echo "Then enter the same password and reboot once more."
  if ! mokutil --import "$MODULE_SIGNING_CERT"; then
    echo -e "${RED}Error: Failed to schedule DAMX MOK enrollment.${NC}"
    return 1
  fi

  echo -e "${YELLOW}MOK enrollment has been scheduled.${NC}"
  echo -e "${YELLOW}Reboot, complete enrollment, then run this installer again.${NC}"
  return 2
}

sign_driver_module() {
  local module_path=$1
  local sign_file
  local signer

  sign_file="/lib/modules/$(uname -r)/build/scripts/sign-file"

  if [ ! -x "$sign_file" ]; then
    echo -e "${RED}Error: Kernel signing tool not found at ${sign_file}.${NC}"
    return 1
  fi
  if [ ! -f "$module_path" ]; then
    echo -e "${RED}Error: Built module not found at ${module_path}.${NC}"
    return 1
  fi

  echo -e "${YELLOW}Signing Linuwu-Sense for Secure Boot...${NC}"
  "$sign_file" sha256 "$MODULE_SIGNING_KEY" "$MODULE_SIGNING_CERT" "$module_path" ||
    return 1

  if command -v modinfo &> /dev/null; then
    signer=$(modinfo -F signer "$module_path" 2>/dev/null || true)
    if [ -z "$signer" ]; then
      echo -e "${RED}Error: The built module does not contain a verifiable signature.${NC}"
      return 1
    fi
    echo -e "${GREEN}Module signed by: ${signer}${NC}"
  fi
}

# Linux 7.2 removed strncpy(). Linuwu-Sense's sysfs store handlers copy a
# bounded prefix of the input and NUL-terminate it themselves, so memcpy()
# keeps their behaviour. strscpy(dst, buf, len) would not: it drops the last
# character, turning a "100,100" fan_speed write into "100,10".
patch_linuwu_sense_sources() {
  local source_file="src/linuwu_sense.c"

  if [ ! -f "$source_file" ]; then
    echo -e "${RED}Error: ${source_file} not found.${NC}"
    return 1
  fi

  if ! grep -q '\bstrncpy(' "$source_file"; then
    return 0
  fi

  echo -e "${YELLOW}Patching Linuwu-Sense for kernels without strncpy()...${NC}"
  sed -i -E 's/\bstrncpy\(([A-Za-z_][A-Za-z0-9_]*), *buf, *len\);/memcpy(\1, buf, len);/' "$source_file"
  if ! grep -q '#include <linux/string.h>' "$source_file"; then
    sed -i -E '0,/#include <linux\/kernel\.h>/s//&\n#include <linux\/string.h>/' "$source_file"
  fi

  if grep -q '\bstrncpy(' "$source_file"; then
    echo -e "${RED}Error: Could not replace every strncpy() call in ${source_file}.${NC}"
    return 1
  fi
}

install_dkms_deps() {
  if command -v pacman &> /dev/null; then
    pacman -S --needed --noconfirm dkms
  elif command -v apt-get &> /dev/null; then
    apt-get update && apt-get install -y dkms
  elif command -v dnf &> /dev/null; then
    dnf install -y dkms
  elif command -v yum &> /dev/null; then
    yum install -y dkms
  elif command -v zypper &> /dev/null; then
    zypper install -y dkms
  else
    return 1
  fi
}

# DKMS falls back to its own unenrolled key when nothing is configured, so
# point it at the DAMX MOK unless the system already has a DKMS signing setup.
configure_dkms_signing() {
  if [ "$MODULE_SIGNING_REQUIRED" != true ]; then
    return 0
  fi

  if grep -qsE '^[[:space:]]*mok_signing_key=' /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf ||
     [ -f /var/lib/dkms/mok.key ]; then
    return 0
  fi

  install -d /etc/dkms/framework.conf.d || return 1
  cat > "$DKMS_SIGNING_CONF" << EOL
mok_signing_key="${MODULE_SIGNING_KEY}"
mok_certificate="${MODULE_SIGNING_CERT}"
EOL
}

# Function to detect and clean up legacy installations
cleanup_legacy_installation() {
  echo -e "${YELLOW}Checking for legacy installations...${NC}"
  local cleanup_performed=false
  local artifact

  # Check for legacy service file (uppercase naming)
  if [ -f "${SYSTEMD_DIR}/${LEGACY_DAEMON_SERVICE_NAME}" ]; then
    echo -e "${BLUE}Found legacy service file: ${LEGACY_DAEMON_SERVICE_NAME}${NC}"

    if systemctl is-active --quiet "${LEGACY_DAEMON_SERVICE_NAME}" 2>/dev/null; then
      echo "Stopping legacy service..."
      systemctl stop "${LEGACY_DAEMON_SERVICE_NAME}"
    fi

    if systemctl is-enabled --quiet "${LEGACY_DAEMON_SERVICE_NAME}" 2>/dev/null; then
      echo "Disabling legacy service..."
      systemctl disable "${LEGACY_DAEMON_SERVICE_NAME}"
    fi

    echo "Removing legacy service file..."
    rm -f "${SYSTEMD_DIR}/${LEGACY_DAEMON_SERVICE_NAME}"
    cleanup_performed=true
  fi

  # Check for legacy installation directory (uppercase naming)
  if [ -d "${LEGACY_INSTALL_DIR}" ]; then
    echo -e "${BLUE}Found legacy installation directory: ${LEGACY_INSTALL_DIR}${NC}"
    echo "Removing legacy installation directory..."
    rm -rf "${LEGACY_INSTALL_DIR}"
    cleanup_performed=true
  fi

  # Check for other potential legacy artifacts
  local legacy_artifacts=(
    "/usr/local/bin/DAMX-Daemon"
    "/usr/share/applications/DAMX.desktop"
    "/usr/share/icons/hicolor/256x256/apps/DAMX.png"
    "${LEGACY_TMPFILES_CONF}"
  )

  for artifact in "${legacy_artifacts[@]}"; do
    if [ -f "$artifact" ] || [ -d "$artifact" ]; then
      echo "Removing legacy artifact: $artifact"
      rm -rf "$artifact"
      cleanup_performed=true
    fi
  done

  if [ "$cleanup_performed" = true ]; then
    echo "Reloading systemd daemon configuration..."
    systemctl daemon-reload
    echo -e "${GREEN}Legacy installation cleanup completed.${NC}"
  else
    echo -e "${GREEN}No legacy installations found.${NC}"
  fi

  return 0
}

# Make sure the toolchain, DKMS and the headers for the *running* kernel are
# present. Previously headers were only installed when `make` was missing, so
# Arch systems with base-devel but no headers could never build the driver.
ensure_build_env() {
  local kernel_release
  local kbuild
  local pm
  local headers_pkg
  local pkgs=()

  kernel_release=$(uname -r)
  kbuild="${KERNEL_MODULES_DIR}/${kernel_release}/build"
  pm=$(detect_pkg_manager || true)

  echo -e "${YELLOW}Checking build environment for kernel ${kernel_release}...${NC}"

  case "$pm" in
    pacman)
      # Always make sure dkms + matching headers are installed on Arch, so
      # pacman's dkms hook rebuilds the module on every kernel update.
      pkgs=(base-devel dkms)
      if headers_pkg=$(arch_headers_pkg); then
        pkgs+=("$headers_pkg")
      else
        echo -e "${YELLOW}Warning: could not find the package that owns the running kernel; install its headers package manually.${NC}"
      fi
      echo "Installing (if needed): ${pkgs[*]}"
      pacman -S --needed --noconfirm "${pkgs[@]}" ||
        echo -e "${YELLOW}Warning: pacman could not install ${pkgs[*]}.${NC}"
      ;;
    apt-get)
      if [ ! -d "$kbuild" ] || ! command -v make &> /dev/null || ! command -v dkms &> /dev/null; then
        apt-get update && apt-get install -y "linux-headers-${kernel_release}" dkms build-essential ||
          echo -e "${YELLOW}Warning: apt-get could not install the build dependencies.${NC}"
      fi
      ;;
    dnf|yum)
      if [ ! -d "$kbuild" ] || ! command -v make &> /dev/null || ! command -v dkms &> /dev/null; then
        "$pm" install -y "kernel-devel-${kernel_release}" dkms gcc make ||
          echo -e "${YELLOW}Warning: ${pm} could not install the build dependencies.${NC}"
      fi
      ;;
    zypper)
      if [ ! -d "$kbuild" ] || ! command -v make &> /dev/null || ! command -v dkms &> /dev/null; then
        zypper install -y kernel-devel dkms gcc make ||
          echo -e "${YELLOW}Warning: zypper could not install the build dependencies.${NC}"
      fi
      ;;
    *)
      echo -e "${YELLOW}Warning: no supported package manager found; build dependencies must already be installed.${NC}"
      ;;
  esac

  if [ ! -d "$kbuild" ]; then
    echo -e "${RED}Error: kernel headers for the running kernel (${kernel_release}) are missing: ${kbuild}${NC}"
    if [ ! -d "${KERNEL_MODULES_DIR}/${kernel_release}" ]; then
      echo "The running kernel's modules directory is gone, so the kernel was probably"
      echo "updated since the last boot. Reboot into the new kernel and run the installer again."
    else
      echo "Install the headers package for your kernel and run the installer again:"
      echo "  Arch:          sudo pacman -S <kernel-package>-headers   (e.g. linux-headers, linux-zen-headers)"
      echo "  Debian/Ubuntu: sudo apt-get install linux-headers-${kernel_release}"
      echo "  Fedora:        sudo dnf install kernel-devel-${kernel_release}"
    fi
    return 1
  fi

  if is_llvm_kernel; then
    echo -e "${YELLOW}Detected LLVM/clang-compiled kernel, using clang...${NC}"
    DRIVER_BUILD_ARGS=(LLVM=1 CC=clang)
    if ! command -v clang &> /dev/null || ! command -v ld.lld &> /dev/null; then
      case "$pm" in
        pacman) pacman -S --needed --noconfirm clang llvm lld ;;
        apt-get) apt-get install -y clang llvm lld ;;
        dnf|yum|zypper) "$pm" install -y clang llvm lld ;;
      esac
    fi
    if ! command -v clang &> /dev/null; then
      echo -e "${RED}Error: the kernel was built with clang but clang is not installed.${NC}"
      return 1
    fi
  else
    DRIVER_BUILD_ARGS=()
  fi

  if ! command -v make &> /dev/null; then
    echo -e "${RED}Error: 'make' is not installed; install your distribution's build tools.${NC}"
    return 1
  fi

  if ! command -v dkms &> /dev/null; then
    echo -e "${YELLOW}Warning: DKMS is not available. The driver will NOT be rebuilt automatically${NC}"
    echo -e "${YELLOW}after kernel updates; install 'dkms' or rerun this installer after each kernel update.${NC}"
  fi

  echo -e "${GREEN}Build environment OK (${kbuild}).${NC}"
  return 0
}

unregister_linuwu_sense_dkms() {
  local version

  if command -v dkms &> /dev/null; then
    for version in /var/lib/dkms/"${LINUWU_SENSE_DKMS_NAME}"/*/; do
      [ -d "$version" ] || continue
      version=$(basename "$version")
      case "$version" in
        kernel-*|original_module) continue ;;
      esac
      if ! dkms remove "${LINUWU_SENSE_DKMS_NAME}/${version}" --all > /dev/null 2>&1; then
        echo -e "${YELLOW}Warning: 'dkms remove ${LINUWU_SENSE_DKMS_NAME}/${version}' failed; removing its tree manually.${NC}"
        rm -rf "/var/lib/dkms/${LINUWU_SENSE_DKMS_NAME}/${version}"
      fi
    done
  fi

  rm -rf "/usr/src/${LINUWU_SENSE_DKMS_NAME}-"*
}

# write_dkms_conf <file> <version>
# Kernels built with clang need LLVM=1 CC=clang. The check is done by DKMS
# for each kernel it builds for (dkms.conf is sourced by bash with
# kernel_source_dir set), falling back to what was detected at install time.
write_dkms_conf() {
  local conf_file=$1
  local version=$2
  local llvm_default=0

  if is_llvm_kernel; then
    llvm_default=1
  fi

  cat > "$conf_file" << EOL
PACKAGE_NAME="${LINUWU_SENSE_DKMS_NAME}"
PACKAGE_VERSION="${version}"
BUILT_MODULE_NAME[0]="linuwu_sense"
BUILT_MODULE_LOCATION[0]="src"
DEST_MODULE_LOCATION[0]="/kernel/drivers/platform/x86"
MAKE[0]="make -C \${kernel_source_dir} M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build modules"
# Generated by the DAMX installer: clang-built kernels need LLVM=1 CC=clang.
if [ -r "\${kernel_source_dir}/.config" ]; then
  if grep -q '^CONFIG_CC_IS_CLANG=y' "\${kernel_source_dir}/.config"; then
    MAKE[0]="\${MAKE[0]} LLVM=1 CC=clang"
  fi
elif [ "${llvm_default}" = "1" ]; then
  MAKE[0]="\${MAKE[0]} LLVM=1 CC=clang"
fi
AUTOINSTALL="yes"
EOL
}

# Register the patched driver sources with DKMS so the module is rebuilt
# automatically for every new kernel. Without it the driver disappears after
# each kernel update. Must be called from the Linuwu-Sense source directory
# after `make install` has succeeded.
register_linuwu_sense_dkms() {
  local kernel_release
  local version
  local dkms_src
  local installed_ko
  local backup_ko

  if ! command -v dkms &> /dev/null; then
    echo -e "${YELLOW}Installing DKMS for automatic driver rebuilds...${NC}"
    if ! install_dkms_deps || ! command -v dkms &> /dev/null; then
      return 1
    fi
  fi

  configure_dkms_signing || return 1

  kernel_release=$(uname -r)
  version=$(get_driver_version .)
  dkms_src="/usr/src/${LINUWU_SENSE_DKMS_NAME}-${version}"

  echo -e "${YELLOW}Registering Linuwu-Sense ${version} with DKMS...${NC}"
  unregister_linuwu_sense_dkms

  install -d "${dkms_src}/src" || return 1
  cp src/*.c "${dkms_src}/src/" || return 1
  cp src/*.h "${dkms_src}/src/" 2> /dev/null || true
  echo 'obj-m := src/linuwu_sense.o' > "${dkms_src}/Kbuild"
  write_dkms_conf "${dkms_src}/dkms.conf" "$version" || return 1

  if ! dkms add "${LINUWU_SENSE_DKMS_NAME}/${version}" ||
     ! dkms build "${LINUWU_SENSE_DKMS_NAME}/${version}" -k "$kernel_release"; then
    unregister_linuwu_sense_dkms
    return 1
  fi

  # `make install` left an unversioned copy in the kernel tree, and DKMS
  # refuses to install over an identical module. Keep it as a fallback until
  # the DKMS copy is in place.
  installed_ko="${KERNEL_MODULES_DIR}/${kernel_release}/kernel/drivers/platform/x86/linuwu_sense.ko"
  backup_ko="${installed_ko}.damx-backup"
  if [ -f "$installed_ko" ]; then
    mv -f "$installed_ko" "$backup_ko" || return 1
  fi

  if ! dkms install "${LINUWU_SENSE_DKMS_NAME}/${version}" -k "$kernel_release"; then
    if [ -f "$backup_ko" ]; then
      mv -f "$backup_ko" "$installed_ko"
      depmod -a "$kernel_release"
    fi
    return 1
  fi

  rm -f "$backup_ko"
  echo -e "${GREEN}Linuwu-Sense will be rebuilt automatically on kernel updates.${NC}"
  return 0
}

# build_driver <source dir>
# Builds (and signs, if needed) the driver without touching the installed
# system, so an update can abort while the old installation still works.
build_driver() {
  local src_dir=$1
  local kernel_release
  local vermagic

  kernel_release=$(uname -r)
  echo -e "${YELLOW}Building Linuwu-Sense $(get_driver_version "$src_dir") from ${src_dir}...${NC}"

  ensure_build_env || return 1

  (
    cd "$src_dir" || exit 1

    patch_linuwu_sense_sources || exit 1

    if ! make clean "${DRIVER_BUILD_ARGS[@]}" || ! make "${DRIVER_BUILD_ARGS[@]}"; then
      echo -e "${RED}Error: Failed to build Linuwu-Sense drivers${NC}"
      exit 1
    fi

    if [ ! -f src/linuwu_sense.ko ]; then
      echo -e "${RED}Error: build finished but src/linuwu_sense.ko was not produced.${NC}"
      exit 1
    fi

    if command -v modinfo &> /dev/null; then
      vermagic=$(modinfo -F vermagic src/linuwu_sense.ko 2> /dev/null | awk '{print $1}')
      if [ -n "$vermagic" ] && [ "$vermagic" != "$kernel_release" ]; then
        echo -e "${RED}Error: built module targets kernel ${vermagic}, but ${kernel_release} is running.${NC}"
        exit 1
      fi
    fi

    if [ "$MODULE_SIGNING_REQUIRED" = true ] &&
       ! sign_driver_module "src/linuwu_sense.ko"; then
      echo -e "${RED}Error: Failed to sign Linuwu-Sense for Secure Boot${NC}"
      exit 1
    fi
  ) || return 1

  echo -e "${GREEN}Linuwu-Sense built successfully.${NC}"
  return 0
}

# install_built_driver <source dir>  (after build_driver succeeded)
install_built_driver() {
  local src_dir=$1

  echo -e "${YELLOW}Installing Linuwu-Sense drivers...${NC}"
  (
    cd "$src_dir" || exit 1

    if ! make install "${DRIVER_BUILD_ARGS[@]}"; then
      echo -e "${RED}Error: Failed to install Linuwu-Sense drivers${NC}"
      exit 1
    fi

    # DKMS is the default whenever it is available.
    if ! register_linuwu_sense_dkms; then
      echo -e "${YELLOW}Warning: DKMS registration failed; the driver will disappear after the next${NC}"
      echo -e "${YELLOW}kernel update. Install 'dkms' and rerun this installer after each kernel update.${NC}"
    fi
  ) || return 1

  echo -e "${GREEN}Linuwu-Sense drivers installed successfully!${NC}"
  return 0
}

# uninstall_drivers [source dir]
# Runs the driver's own `make uninstall` when a source tree is available
# (logging, not hiding, its errors), then removes everything by hand so the
# module, .ko and DKMS entry are gone even if `make uninstall` failed.
uninstall_drivers() {
  local src_dir=${1:-}
  local output
  local ko
  local unit="linuwu_sense.service"

  echo "Uninstalling Linuwu-Sense drivers..."

  if [ -n "$src_dir" ] && [ -f "${src_dir}/Makefile" ]; then
    if ! output=$(cd "$src_dir" && make uninstall 2>&1); then
      echo -e "${YELLOW}Warning: 'make uninstall' reported errors (continuing with manual cleanup):${NC}"
    fi
    if [ -n "$output" ]; then
      printf '%s\n' "$output" | sed 's/^/  [make uninstall] /'
    fi
  else
    echo "No driver source tree available; performing manual driver cleanup."
  fi

  unregister_linuwu_sense_dkms

  if [ -d /sys/module/linuwu_sense ]; then
    if ! rmmod linuwu_sense 2> /dev/null && ! modprobe -r linuwu_sense; then
      echo -e "${YELLOW}Warning: could not unload linuwu_sense (in use?). It will be gone after a reboot.${NC}"
    fi
  fi

  for ko in "${KERNEL_MODULES_DIR}"/*/kernel/drivers/platform/x86/linuwu_sense.ko \
            "${KERNEL_MODULES_DIR}"/*/kernel/drivers/platform/x86/linuwu_sense.ko.damx-backup; do
    if [ -e "$ko" ]; then
      echo "Removing $ko"
      rm -f "$ko"
    fi
  done

  if systemctl is-enabled --quiet "$unit" 2> /dev/null || systemctl is-active --quiet "$unit" 2> /dev/null; then
    systemctl disable --now "$unit" 2> /dev/null || true
  fi
  rm -f "${SYSTEMD_DIR}/${unit}" \
        /etc/modules-load.d/linuwu_sense.conf \
        /etc/modprobe.d/blacklist-acer_wmi.conf \
        "$LEGACY_TMPFILES_CONF" \
        "$LINUWU_SENSE_UDEV_RULE"
  if command -v udevadm &> /dev/null; then
    udevadm control --reload 2> /dev/null || true
  fi
  depmod -a 2> /dev/null || true
  modprobe acer_wmi 2> /dev/null || true
}

# The daemon makes its socket root:damx 0660 when the group exists.
setup_damx_group() {
  if ! getent group "$DAMX_GROUP" > /dev/null; then
    echo "Creating system group '${DAMX_GROUP}'..."
    if ! groupadd --system "$DAMX_GROUP"; then
      echo -e "${RED}Error: could not create group '${DAMX_GROUP}'.${NC}"
      return 1
    fi
  fi

  if [ -z "$REAL_USER" ]; then
    echo -e "${YELLOW}Warning: could not determine which user should use DAMX.${NC}"
    echo -e "${YELLOW}Add it to the group manually: sudo usermod -aG ${DAMX_GROUP} <user>${NC}"
    return 0
  fi

  if id -nG "$REAL_USER" | tr ' ' '\n' | grep -qx "$DAMX_GROUP"; then
    echo "User ${REAL_USER} is already in group '${DAMX_GROUP}'."
    return 0
  fi

  if ! usermod -aG "$DAMX_GROUP" "$REAL_USER"; then
    echo -e "${RED}Error: could not add ${REAL_USER} to group '${DAMX_GROUP}'.${NC}"
    return 1
  fi
  echo "Added ${REAL_USER} to group '${DAMX_GROUP}'."
  DAMX_GROUP_ADDED=true
}

print_group_note() {
  if [ "$DAMX_GROUP_ADDED" = true ]; then
    echo ""
    echo -e "${YELLOW}IMPORTANT: ${REAL_USER} was added to the '${DAMX_GROUP}' group.${NC}"
    echo -e "${YELLOW}Log out and back in (or run 'newgrp ${DAMX_GROUP}' in a terminal) before${NC}"
    echo -e "${YELLOW}the DAMX GUI can connect to the daemon.${NC}"
  fi
}

print_group_removal_note() {
  if getent group "$DAMX_GROUP" > /dev/null; then
    echo -e "${BLUE}The '${DAMX_GROUP}' group was left in place. To remove it: sudo groupdel ${DAMX_GROUP}${NC}"
  fi
}

install_daemon() {
  echo -e "${YELLOW}Installing DAMX-Daemon...${NC}"

  if [ ! -f "DAMX-Daemon/DAMX-Daemon" ]; then
    echo -e "${RED}Error: DAMX-Daemon/DAMX-Daemon not found!${NC}"
    echo "Please make sure the installer is run from the directory containing the DAMX-Daemon folder."
    pause
    return 1
  fi

  if ! mkdir -p "${INSTALL_DIR}/daemon" ||
     ! cp -f DAMX-Daemon/DAMX-Daemon "${INSTALL_DIR}/daemon/" ||
     ! chmod 755 "${INSTALL_DIR}/daemon/DAMX-Daemon"; then
    echo -e "${RED}Error: failed to copy DAMX-Daemon to ${INSTALL_DIR}/daemon; service not enabled.${NC}"
    return 1
  fi

  # The daemon removes stale sockets itself and sets the socket to
  # root:damx 0660, so no ExecStartPre is needed.
  if ! cat > "${SYSTEMD_DIR}/${DAEMON_SERVICE_NAME}" << EOL
[Unit]
Description=DAMX Daemon for Acer laptops
After=systemd-modules-load.service

[Service]
Type=simple
ExecStart=${INSTALL_DIR}/daemon/DAMX-Daemon
Restart=on-failure
RestartSec=5
User=root
RuntimeDirectory=damx
RuntimeDirectoryMode=0700
StateDirectory=damx
StateDirectoryMode=0755
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOL
  then
    echo -e "${RED}Error: failed to write ${SYSTEMD_DIR}/${DAEMON_SERVICE_NAME}.${NC}"
    return 1
  fi

  systemctl daemon-reload
  if ! systemctl enable "${DAEMON_SERVICE_NAME}"; then
    echo -e "${RED}Error: failed to enable ${DAEMON_SERVICE_NAME}.${NC}"
    return 1
  fi
  systemctl restart "${DAEMON_SERVICE_NAME}"

  if systemctl is-active --quiet "${DAEMON_SERVICE_NAME}"; then
    echo -e "${GREEN}DAMX-Daemon installed and service started successfully!${NC}"
    return 0
  else
    echo -e "${RED}Warning: DAMX-Daemon service may not have started correctly. Check with 'systemctl status ${DAEMON_SERVICE_NAME}'${NC}"
    return 1
  fi
}

install_gui() {
  echo -e "${YELLOW}Installing DAMX-GUI...${NC}"

  if [ ! -d "DAMX-GUI" ]; then
    echo -e "${RED}Error: DAMX-GUI directory not found!${NC}"
    echo "Please make sure the installer is run from the directory containing the DAMX-GUI folder."
    pause
    return 1
  fi

  if ! mkdir -p "${INSTALL_DIR}/gui" ||
     ! cp -rf DAMX-GUI/. "${INSTALL_DIR}/gui/" ||
     ! chmod +x "${INSTALL_DIR}/gui/DivAcerManagerMax"; then
    echo -e "${RED}Error: failed to copy DAMX-GUI to ${INSTALL_DIR}/gui.${NC}"
    return 1
  fi

  mkdir -p "${ICON_DIR}"
  cp -f DAMX-GUI/icon.png "${ICON_DIR}/damx.png" ||
    echo -e "${YELLOW}Warning: could not install the DAMX icon.${NC}"

  cat > "${DESKTOP_FILE_DIR}/damx.desktop" << EOL
[Desktop Entry]
Name=DAMX
Comment=Div Acer Manager Max
Exec=${INSTALL_DIR}/gui/DivAcerManagerMax
Icon=damx
Terminal=false
Type=Application
Categories=Utility;System;
Keywords=acer;laptop;system;
EOL

  cat > "${BIN_DIR}/DAMX" << EOL
#!/bin/bash
${INSTALL_DIR}/gui/DivAcerManagerMax "\$@"
EOL
  chmod +x "${BIN_DIR}/DAMX"

  echo -e "${GREEN}DAMX-GUI installed successfully!${NC}"
  return 0
}

# Function to check system compatibility
check_system() {
  echo -e "${BLUE}Checking system compatibility...${NC}"

  if ! command -v systemctl &> /dev/null; then
    echo -e "${RED}Error: systemd is required but not found on this system.${NC}"
    return 1
  fi

  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "Detected OS: ${PRETTY_NAME:-unknown}"
  fi

  echo -e "${GREEN}System compatibility check passed.${NC}"
  return 0
}

main_menu() {
  local choice
  local rc

  if ! check_system; then
    echo -e "${RED}System compatibility check failed. Exiting.${NC}"
    pause
    exit 1
  fi

  while true; do
    print_banner

    echo -e "Please select an option:"
    echo -e "  ${GREEN}1${NC}) Install DAMX Suite (complete)"
    echo -e "  ${GREEN}2${NC}) Install DAMX Suite (without drivers)"
    echo -e "  ${GREEN}3${NC}) Uninstall DAMX Suite"
    echo -e "  ${GREEN}4${NC}) Reinstall/Update DAMX Suite (recommended for upgrades)"
    echo -e "  ${GREEN}5${NC}) Check service status"
    echo -e "  ${GREEN}q${NC}) Quit"
    echo ""

    read -r -p "Enter your choice [1-5 or q]: " choice

    case $choice in
      1)
        print_banner
        echo -e "${BLUE}Starting complete installation...${NC}"
        perform_install false false
        rc=$?
        [ "$rc" -eq 0 ] || exit "$rc"
        ;;
      2)
        print_banner
        echo -e "${BLUE}Starting installation without drivers...${NC}"
        perform_install true false
        rc=$?
        [ "$rc" -eq 0 ] || exit "$rc"
        ;;
      3)
        print_banner
        echo -e "${BLUE}Starting uninstallation...${NC}"
        uninstall
        ;;
      4)
        print_banner
        echo -e "${BLUE}Starting reinstallation/update...${NC}"
        echo -e "${YELLOW}The new driver is built first; the existing installation is only replaced if the build succeeds.${NC}"
        perform_install false true
        rc=$?
        [ "$rc" -eq 0 ] || exit "$rc"
        ;;
      5)
        print_banner
        echo -e "${BLUE}Checking DAMX service status...${NC}"
        echo ""
        if systemctl list-unit-files | grep -q "${DAEMON_SERVICE_NAME}"; then
          systemctl status "${DAEMON_SERVICE_NAME}" --no-pager -l
        else
          echo -e "${YELLOW}DAMX service not found. The suite may not be installed.${NC}"
        fi
        echo ""
        pause
        ;;
      q|Q)
        echo -e "${BLUE}Exiting installer. Goodbye!${NC}"
        exit 0
        ;;
      *)
        echo -e "${RED}Invalid option. Please try again.${NC}"
        sleep 2
        ;;
    esac
  done
}

cleanup_workdir() {
  if [ -n "${WORKDIR:-}" ] && [ -d "$WORKDIR" ]; then
    rm -rf "$WORKDIR"
  fi
}

make_workdir() {
  WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/damx-install.XXXXXX") || {
    echo -e "${RED}Error: could not create a temporary working directory.${NC}"
    exit 1
  }
  trap cleanup_workdir EXIT
  trap 'exit 130' INT TERM
}

# comprehensive_cleanup <driver source dir or ""> <skip_drivers>
comprehensive_cleanup() {
  local driver_dir=${1:-}
  local skip_drivers=${2:-false}

  echo -e "${YELLOW}Performing comprehensive cleanup...${NC}"

  if systemctl is-active --quiet "${DAEMON_SERVICE_NAME}" 2>/dev/null; then
    echo "Stopping current DAMX-Daemon service..."
    systemctl stop "${DAEMON_SERVICE_NAME}"
  fi

  if systemctl is-enabled --quiet "${DAEMON_SERVICE_NAME}" 2>/dev/null; then
    echo "Disabling current DAMX-Daemon service..."
    systemctl disable "${DAEMON_SERVICE_NAME}"
  fi

  if [ -f "${SYSTEMD_DIR}/${DAEMON_SERVICE_NAME}" ]; then
    echo "Removing current service file..."
    rm -f "${SYSTEMD_DIR}/${DAEMON_SERVICE_NAME}"
  fi

  cleanup_legacy_installation

  echo "Removing current installation files..."
  rm -rf "${INSTALL_DIR}"
  rm -f "${BIN_DIR}/DAMX"
  rm -f "${DESKTOP_FILE_DIR}/damx.desktop"
  rm -f "${ICON_DIR}/damx.png"

  if [ "$skip_drivers" != true ]; then
    uninstall_drivers "$driver_dir"
    rm -f "$DKMS_SIGNING_CONF"
  fi

  systemctl daemon-reload

  echo -e "${GREEN}Comprehensive cleanup completed.${NC}"
  return 0
}

# http_get <url> <output file>
http_get() {
  if command -v curl &> /dev/null; then
    curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget &> /dev/null; then
    wget -qO "$2" "$1"
  else
    echo -e "${RED}curl or wget is required to download DAMX.${NC}" >&2
    return 1
  fi
}

# Prints the download URLs of all assets in a GitHub release JSON file.
release_asset_urls() {
  grep '"browser_download_url"' "$1" | cut -d '"' -f 4
}

# verify_release_archive <archive> <release json>
verify_release_archive() {
  local archive=$1
  local release_json=$2
  local file_name
  local sum_url
  local sum_file
  local expected
  local actual

  file_name=$(basename "$archive")

  sum_url=$(release_asset_urls "$release_json" |
    awk -v want="${file_name}.sha256" '{ n = $0; sub(/.*\//, "", n) } n == want { print; exit }')
  if [ -z "$sum_url" ]; then
    sum_url=$(release_asset_urls "$release_json" |
      awk '{ n = tolower($0); sub(/.*\//, "", n) } n == "sha256sums" || n == "sha256sums.txt" || n == "checksums.txt" { print; exit }')
  fi

  if [ -n "$sum_url" ]; then
    sum_file="${WORKDIR}/checksum.txt"
    if ! http_get "$sum_url" "$sum_file"; then
      echo -e "${RED}Error: failed to download checksum file ${sum_url}${NC}"
      return 1
    fi
    expected=$(awk -v f="$file_name" '
      NF == 1 && NR == 1 { single = $1 }
      NF >= 2 { n = $2; sub(/^\*/, "", n); if (n == f) { print $1; found = 1; exit } }
      END { if (!found && single != "") print single }' "$sum_file")
    actual=$(sha256sum "$archive" | awk '{print $1}')
    if [ -z "$expected" ] || [ "${expected,,}" != "${actual,,}" ]; then
      echo -e "${RED}Error: SHA-256 mismatch for ${file_name}.${NC}"
      echo "  expected: ${expected:-<not found in checksum file>}"
      echo "  actual:   ${actual}"
      return 1
    fi
    echo -e "${GREEN}SHA-256 verified: ${actual}${NC}"
  else
    echo -e "${YELLOW}No checksum asset published for this release; checking archive integrity only.${NC}"
  fi

  if ! tar -tJf "$archive" > /dev/null; then
    echo -e "${RED}Error: ${file_name} is not a valid .tar.xz archive (corrupt or incomplete download).${NC}"
    return 1
  fi
  return 0
}

# Always downloads a fresh copy of the latest release into $WORKDIR.
# Sets RELEASE_TARBALL.
download_latest_release() {
  local api_url="https://api.github.com/repos/${DAMX_REPO}/releases/latest"
  local release_json="${WORKDIR}/release.json"
  local tar_url

  RELEASE_TARBALL=""
  echo -e "${YELLOW}Fetching latest DAMX release info from GitHub...${NC}"
  if ! http_get "$api_url" "$release_json"; then
    echo -e "${RED}Failed to fetch release info from ${api_url}${NC}"
    return 1
  fi

  tar_url=$(release_asset_urls "$release_json" | grep -E '/DAMX-[^/]*\.tar\.xz$' | head -n 1)
  if [ -z "$tar_url" ]; then
    echo -e "${RED}No DAMX-<tag>.tar.xz asset found in latest release!${NC}"
    return 1
  fi

  RELEASE_TARBALL="${WORKDIR}/$(basename "$tar_url")"
  echo -e "${YELLOW}Downloading ${tar_url}...${NC}"
  if ! http_get "$tar_url" "$RELEASE_TARBALL"; then
    echo -e "${RED}Failed to download $(basename "$tar_url")${NC}"
    return 1
  fi

  verify_release_archive "$RELEASE_TARBALL" "$release_json"
}

# Downloads, verifies and extracts the release. Sets RELEASE_ROOT.
prepare_and_extract_release() {
  local extract_dir="${WORKDIR}/release"

  RELEASE_ROOT=""
  if ! download_latest_release; then
    echo -e "${RED}Could not obtain a valid DAMX release archive.${NC}"
    pause
    return 1
  fi

  mkdir -p "$extract_dir"
  echo -e "${YELLOW}Extracting $(basename "$RELEASE_TARBALL")...${NC}"
  if ! tar -xJf "$RELEASE_TARBALL" -C "$extract_dir"; then
    echo -e "${RED}Failed to extract $(basename "$RELEASE_TARBALL").${NC}"
    pause
    return 1
  fi

  # Release archives wrap their contents in a DAMX-<version>/ directory.
  RELEASE_ROOT=$(find "$extract_dir" -maxdepth 2 -type d -name DAMX-Daemon -printf '%h\n' | head -n 1)
  if [ -z "$RELEASE_ROOT" ] || [ ! -d "$RELEASE_ROOT/DAMX-GUI" ]; then
    echo -e "${RED}DAMX-GUI and DAMX-Daemon were not found in the release archive.${NC}"
    pause
    return 1
  fi

  echo -e "${GREEN}Release extracted to ${RELEASE_ROOT}.${NC}"
}

# Prefers the Linuwu-Sense/ tree bundled in the release (Div-Linuwu-Sense);
# only clones LINUWU_SENSE_REPO at LINUWU_SENSE_REF if it is missing.
# Sets DRIVER_SOURCE_DIR.
obtain_driver_source() {
  local dest="${WORKDIR}/Linuwu-Sense-git"
  local url="https://github.com/${LINUWU_SENSE_REPO}.git"
  local commit

  DRIVER_SOURCE_DIR=""

  if [ -n "$RELEASE_ROOT" ] && [ -f "${RELEASE_ROOT}/Linuwu-Sense/src/linuwu_sense.c" ]; then
    DRIVER_SOURCE_DIR="${RELEASE_ROOT}/Linuwu-Sense"
    echo -e "${BLUE}Using the driver bundled with the release (DRIVER_VERSION $(get_driver_version "$DRIVER_SOURCE_DIR")).${NC}"
    return 0
  fi

  echo -e "${YELLOW}The release does not bundle Linuwu-Sense; cloning ${LINUWU_SENSE_REPO} (ref: ${LINUWU_SENSE_REF:-default branch})...${NC}"
  if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: git is required to download the driver. Install git and try again.${NC}"
    return 1
  fi

  rm -rf "$dest"
  if [ -n "$LINUWU_SENSE_REF" ]; then
    if ! git init -q "$dest" ||
       ! git -C "$dest" remote add origin "$url" ||
       ! git -C "$dest" fetch -q --depth 1 origin "$LINUWU_SENSE_REF" ||
       ! git -C "$dest" checkout -q --detach FETCH_HEAD; then
      echo -e "${RED}Failed to fetch ${LINUWU_SENSE_REPO} at ${LINUWU_SENSE_REF}!${NC}"
      return 1
    fi
  elif ! git clone -q --depth 1 "$url" "$dest"; then
    echo -e "${RED}Failed to clone ${LINUWU_SENSE_REPO}!${NC}"
    return 1
  fi

  if [ ! -f "${dest}/src/linuwu_sense.c" ]; then
    echo -e "${RED}Error: ${LINUWU_SENSE_REPO} does not contain src/linuwu_sense.c.${NC}"
    return 1
  fi

  commit=$(git -C "$dest" rev-parse HEAD)
  DRIVER_SOURCE_DIR="$dest"
  echo -e "${BLUE}Using ${LINUWU_SENSE_REPO} commit ${commit} (DRIVER_VERSION $(get_driver_version "$dest")).${NC}"
  return 0
}

perform_install() {
  local skip_drivers=$1
  local is_update=$2
  local signing_status
  local driver_result=0
  local daemon_result
  local gui_result

  # Do this before cleanup so a pending MOK enrollment never removes an
  # otherwise working installation.
  if [ "$skip_drivers" = false ]; then
    prepare_secure_boot_signing
    signing_status=$?
    if [ $signing_status -ne 0 ]; then
      if [ $signing_status -eq 2 ]; then
        pause
      fi
      return $signing_status
    fi
  fi

  # Likewise, make sure the release is usable before removing anything.
  prepare_and_extract_release || return 1
  cd "$RELEASE_ROOT" || return 1

  # Build the driver before touching the existing installation.
  if [ "$skip_drivers" = false ]; then
    if ! obtain_driver_source || ! build_driver "$DRIVER_SOURCE_DIR"; then
      echo -e "${RED}Driver build failed; the existing installation was left untouched.${NC}"
      pause
      return 1
    fi
  fi

  if [ "$is_update" = true ]; then
    echo -e "${BLUE}Removing the previous installation...${NC}"
    comprehensive_cleanup "$DRIVER_SOURCE_DIR" "$skip_drivers"
    echo ""
  else
    cleanup_legacy_installation
    echo ""
  fi

  mkdir -p "${INSTALL_DIR}"

  if [ "$skip_drivers" = false ]; then
    install_built_driver "$DRIVER_SOURCE_DIR"
    driver_result=$?
    if [ $driver_result -ne 0 ]; then
      echo -e "${RED}Driver installation failed; daemon and GUI installation were not attempted.${NC}"
      pause
      return $driver_result
    fi
  else
    echo -e "${YELLOW}Skipping driver installation as requested.${NC}"
  fi

  # Must exist before the daemon starts so it can hand the socket to the group.
  setup_damx_group

  install_daemon
  daemon_result=$?

  install_gui
  gui_result=$?

  if [ $driver_result -eq 0 ] && [ $daemon_result -eq 0 ] && [ $gui_result -eq 0 ]; then
    echo -e "${GREEN}DAMX Suite installation completed successfully!${NC}"
    echo -e "You can now run the GUI using the ${BLUE}DAMX${NC} command or from your application launcher."
    print_group_note
    echo ""
    echo -e "${BLUE}Service Status:${NC}"
    systemctl status "${DAEMON_SERVICE_NAME}" --no-pager -l
    pause
    return 0
  else
    echo -e "${RED}Some components failed to install. Please check the errors above.${NC}"
    print_group_note
    pause
    return 1
  fi
}

uninstall() {
  echo -e "${YELLOW}Uninstalling DAMX Suite...${NC}"
  # No driver source tree here: uninstall_drivers removes everything by hand.
  comprehensive_cleanup "" false
  echo -e "${GREEN}DAMX Suite uninstalled successfully!${NC}"
  print_group_removal_note
  pause
  return 0
}

main() {
  if [ "$EUID" -ne 0 ]; then
    echo -e "${YELLOW}This script requires root privileges.${NC}"
    if [ -f "$0" ] && command -v sudo &> /dev/null; then
      echo -e "${BLUE}Attempting to run with sudo...${NC}"
      # `bash` explicitly, so it works without the exec bit.
      exec sudo bash "$(readlink -f "$0")" "$@"
    fi
    echo -e "${RED}Error: please run this installer as root (e.g. with sudo).${NC}"
    exit 1
  fi

  REAL_USER=$(resolve_real_user)
  # Never work in (or delete things from) the caller's CWD.
  make_workdir
  cd "$WORKDIR" || exit 1
  main_menu
}

# Run only when executed (including `curl ... | sudo bash`), not when sourced.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
  exit 0
fi
