#!/bin/bash

# SPDX-FileCopyrightText: 2025 Henrik Sandklef
#
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Looks for unknown licenses in a file with license (expressions)
#     check-license-file.sh <LICENSE-FILE>
# 

cat $1 | while read LICENSE
do
    printf "unknown\n$LICENSE\n" 
done | ./devel/flame shell -s | grep -v Unknown
