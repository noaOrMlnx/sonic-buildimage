# Mellanox phy debug tools scripts

DEBUG_TOOLS_VERSION = 1.0.0
MLNX_PHY_DEBUG_TOOLS = mlnx-phy-debug-tools_$(DEBUG_TOOLS_VERSION)_amd64.deb
$(MLNX_PHY_DEBUG_TOOLS)_SRC_PATH = $(PLATFORM_PATH)/mlnx-phy-debug-tools

SONIC_DPKG_DEBS += $(MLNX_PHY_DEBUG_TOOLS)

export MLNX_PHY_DEBUG_TOOLS
