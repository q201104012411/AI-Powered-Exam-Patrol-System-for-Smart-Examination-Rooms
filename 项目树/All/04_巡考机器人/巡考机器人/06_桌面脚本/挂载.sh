#!/bin/bash
# 一键挂载：小车文件 + 共享文件夹
ok=0
if ! mount | grep -q "/home/wheeltec/car_files"; then
    mkdir -p /home/wheeltec/car_files
    umount -l /home/wheeltec/car_files 2>/dev/null
    if sshfs wheeltec@172.19.12.66:/home/wheeltec/patrol_robot /home/wheeltec/car_files \
        -o reconnect,ServerAliveInterval=10,ServerAliveCountMax=3 2>/dev/null; then
        echo "[OK] 小车文件已挂载"; ok=1
    else
        echo "[失败] 小车连不上（检查是否开机 / WiFi）"; ok=0
    fi
else
    echo "[跳过] 小车文件已挂载"; ok=1
fi

if ! mount | grep -q "/mnt/hgfs/z"; then
    echo dongguan | sudo -S mkdir -p /mnt/hgfs/z 2>/dev/null
    if echo dongguan | sudo -S vmhgfs-fuse .host:/z /mnt/hgfs/z -o allow_other 2>/dev/null; then
        echo "[OK] 共享文件夹已挂载"
    else
        echo "[失败] 共享文件夹挂载失败"
    fi
else
    echo "[跳过] 共享文件夹已挂载"
fi

sleep 0.5
nautilus /home/wheeltec/car_files >/dev/null 2>&1 &
exit 0

echo
read -p "按回车关闭窗口..." _
