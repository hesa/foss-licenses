#!/bin/bash

# SPDX-FileCopyrightText: 2025 Henrik Sandklef
#
# SPDX-License-Identifier: GPL-3.0-or-later

exec_flame()
{
    echo " ####exec    ./devel/flame $*" >> /tmp/cmds.txt

    ./devel/flame $*
}


test_flame()
{
    COMMAND="$1"
    LICENSE="$2"
    EXPECTED="$3"
    EXPECTED_RET=$4

    if [ "$EXPECTED_RET" = "" ]
    then
        EXPECTED_RET=0
    fi

    set -o pipefail
    ACTUAL=$(exec_flame $COMMAND "$LICENSE" 2>/dev/null | head -1)
    RET=$?

    echo -n "   flame $COMMAND $LICENSE: "
    if [ $RET -ne $EXPECTED_RET ]
    then
        echo
        echo "fail....."
        echo "  license :           $LICENSE"
        echo "  expected exit code: $EXPECTED_RET"
        echo "  actual exit code:   $RET"
        exit 1
    fi

    if [ "$ACTUAL" != "$EXPECTED" ]
    then
        echo
        echo "fail....."
        echo "  license : $LICENSE"
        echo "  expected: $EXPECTED" 
        echo "  actual:   $ACTUAL"
        exit 1
    fi
    echo "OK"
}


echo "Testing issues"
echo "=============="
echo " https://github.com/hesa/foss-licenses/issues/214"
echo " ---------------------------------------------------"
test_flame license "GPL" "GPL-1.0-only OR GPL-2.0-only OR GPL-3.0-only"
echo

echo " https://github.com/hesa/foss-licenses/issues/239"
echo " ---------------------------------------------------"
test_flame license "Eclipse Public License -v 1.0" "EPL-1.0"
test_flame unknown "Eclipse Public License -v 1.0" "OK"
test_flame unknown "Eclipse Public License -v -v" "Error: Unknown symbols identified." 1
test_flame license "what is this -v -v" "what is this -v -v" 0
echo


