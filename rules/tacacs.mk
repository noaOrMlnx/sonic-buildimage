# libpam-tacplus packages

PAM_TACPLUS_VERSION = 1.4.1-1

export PAM_TACPLUS_VERSION

LIBTAC2 = libtac2_$(PAM_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBTAC2)_SRC_PATH = $(SRC_PATH)/tacacs/pam
SONIC_MAKE_DEBS += $(LIBTAC2)

LIBTAC2_DBG = libtac2-dbgsym_$(PAM_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBTAC2_DBG)_DEPENDS += $(LIBTAC2)
$(LIBTAC2_DBG)_RDEPENDS += $(LIBTAC2)
$(eval $(call add_derived_package,$(LIBTAC2),$(LIBTAC2_DBG)))

LIBPAM_TACPLUS = libpam-tacplus_$(PAM_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBPAM_TACPLUS)_RDEPENDS += $(LIBTAC2)
$(eval $(call add_extra_package,$(LIBTAC2),$(LIBPAM_TACPLUS)))

LIBPAM_TACPLUS_DBG = libpam-tacplus-dbgsym_$(PAM_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBPAM_TACPLUS_DBG)_DEPENDS += $(LIBPAM_TACPLUS)
$(LIBPAM_TACPLUS_DBG)_RDEPENDS += $(LIBPAM_TACPLUS)
$(eval $(call add_derived_package,$(LIBTAC2),$(LIBPAM_TACPLUS_DBG)))

LIBTAC_DEV = libtac-dev_$(PAM_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBTAC_DEV)_DEPENDS += $(LIBTAC2)
$(eval $(call add_derived_package,$(LIBTAC2),$(LIBTAC_DEV)))

# libnss-tacplus packages
NSS_TACPLUS_VERSION = 1.0.4-1

export NSS_TACPLUS_VERSION

LIBNSS_TACPLUS = libnss-tacplus_$(NSS_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBNSS_TACPLUS)_DEPENDS += $(LIBTAC_DEV)
$(LIBNSS_TACPLUS)_RDEPENDS += $(LIBTAC2)
$(LIBNSS_TACPLUS)_SRC_PATH = $(SRC_PATH)/tacacs/nss
SONIC_MAKE_DEBS += $(LIBNSS_TACPLUS)

LIBNSS_TACPLUS_DBG = libnss-tacplus-dbgsym_$(NSS_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(LIBNSS_TACPLUS_DBG)_DEPENDS += $(LIBNSS_TACPLUS)
$(LIBNSS_TACPLUS_DBG)_RDEPENDS += $(LIBNSS_TACPLUS)
$(eval $(call add_derived_package,$(LIBNSS_TACPLUS),$(LIBNSS_TACPLUS_DBG)))

# audisp-tacplus packages
AUDISP_TACPLUS_VERSION = 1.0.2

export AUDISP_TACPLUS_VERSION

AUDISP_TACPLUS = audisp-tacplus_$(AUDISP_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(AUDISP_TACPLUS)_DEPENDS += $(LIBTAC_DEV)
$(AUDISP_TACPLUS)_RDEPENDS += $(LIBTAC2)
$(AUDISP_TACPLUS)_SRC_PATH = $(SRC_PATH)/tacacs/audisp
SONIC_MAKE_DEBS += $(AUDISP_TACPLUS)

AUDISP_TACPLUS_DBG = audisp-tacplus-dbgsym_$(AUDISP_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(AUDISP_TACPLUS_DBG)_DEPENDS += $(AUDISP_TACPLUS)
$(AUDISP_TACPLUS_DBG)_RDEPENDS += $(AUDISP_TACPLUS)
$(eval $(call add_derived_package,$(AUDISP_TACPLUS),$(AUDISP_TACPLUS_DBG)))

# bash-tacplus packages
BASH_TACPLUS_VERSION = 1.0.0

export BASH_TACPLUS_VERSION

BASH_TACPLUS = bash-tacplus_$(BASH_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(BASH_TACPLUS)_DEPENDS += $(LIBTAC_DEV)
$(BASH_TACPLUS)_RDEPENDS += $(LIBTAC2)
$(BASH_TACPLUS)_SRC_PATH = $(SRC_PATH)/tacacs/bash_tacplus
SONIC_DPKG_DEBS += $(BASH_TACPLUS)

BASH_TACPLUS_DBG = bash-tacplus-dbgsym_$(BASH_TACPLUS_VERSION)_$(CONFIGURED_ARCH).deb
$(BASH_TACPLUS_DBG)_DEPENDS += $(BASH_TACPLUS)
$(BASH_TACPLUS_DBG)_RDEPENDS += $(BASH_TACPLUS)
$(eval $(call add_derived_package,$(BASH_TACPLUS),$(BASH_TACPLUS_DBG)))

# The .c, .cpp, .h & .hpp files under src/{$DBG_SRC_ARCHIVE list}
# are archived into debug one image to facilitate debugging.
#
DBG_SRC_ARCHIVE += tacacs
