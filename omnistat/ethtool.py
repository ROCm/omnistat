# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------

"""Driver-level NIC statistics over the SIOCETHTOOL ioctl.

Exposes the counters from the device driver and are independent of the netdev
totals published under /sys/class/net/<interface>/statistics. The ioctls are
unprivileged and do not need the ethtool binary.
"""

import array
import fcntl
import socket
import struct

# ABI constants from linux/sockios.h and linux/ethtool.h, which Python does not
# expose. SIOCETHTOOL carries every ethtool command; the command number opening
# each request selects which one.
SIOCETHTOOL = 0x8946

ETHTOOL_GSSET_INFO = 0x37
ETHTOOL_GSTRINGS = 0x1B
ETHTOOL_GSTATS = 0x1D

ETH_SS_STATS = 1
ETH_GSTRING_LEN = 32


class Ethtool:
    """SIOCETHTOOL reader, holding the socket the ioctls are issued on.

    Resolve counters once with indices(), then sample them with values(): the
    counter list is fixed while the driver is bound, so a sample costs one ioctl.
    """

    def __init__(self):
        self.__socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.__counts = {}

    def __ioctl(self, interface, command, request=b"", reply_size=0):
        """Issue one SIOCETHTOOL call and return the buffer the kernel filled.

        Every command is a struct opening with the command number; request holds
        its remaining input fields, reply_size the variable-length array after them.
        """
        buffer = array.array("B", struct.pack("=I", command) + request + bytes(reply_size))
        # ifreq pairs the interface name with a pointer to that buffer, which
        # the kernel reads and overwrites in place.
        ifreq = struct.pack("16sP", interface.encode(), buffer.buffer_info()[0])
        fcntl.ioctl(self.__socket.fileno(), SIOCETHTOOL, ifreq)
        return buffer.tobytes()

    def __count(self, interface):
        """Number of counters the interface exposes, 0 if it has none."""
        if interface not in self.__counts:
            reply = self.__ioctl(interface, ETHTOOL_GSSET_INFO, struct.pack("=IQI", 0, 1 << ETH_SS_STATS, 0))
            mask, count = struct.unpack("=QI", reply[8:20])
            # A cleared mask means the device does not implement the set at all.
            self.__counts[interface] = count if mask else 0
        return self.__counts[interface]

    def indices(self, interface, counters):
        """Positions of the named counters in values(), or None if any is missing."""
        count = self.__count(interface)
        if count == 0:
            return None
        reply = self.__ioctl(
            interface, ETHTOOL_GSTRINGS, struct.pack("=II", ETH_SS_STATS, count), count * ETH_GSTRING_LEN
        )
        names = [
            reply[offset : offset + ETH_GSTRING_LEN].split(b"\0")[0].decode("ascii", "replace")
            for offset in range(12, 12 + count * ETH_GSTRING_LEN, ETH_GSTRING_LEN)
        ]
        try:
            return tuple(names.index(counter) for counter in counters)
        except ValueError:
            return None

    def values(self, interface):
        """Every counter the interface exposes, in the order indices() refers to."""
        count = self.__count(interface)
        reply = self.__ioctl(interface, ETHTOOL_GSTATS, struct.pack("=I", count), count * 8)
        return struct.unpack(f"={count}Q", reply[8 : 8 + count * 8])
