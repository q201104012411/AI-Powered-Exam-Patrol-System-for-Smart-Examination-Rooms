# -*- coding: utf-8 -*-
from __future__ import print_function
import math, numpy as np, rospy
from sensor_msgs.msg import Image, Imu, LaserScan
from nav_msgs.msg import Odometry
rospy.init_node("measure", anonymous=True, disable_signals=True)
d = rospy.wait_for_message("/camera/depth/image_raw", Image, timeout=10)
img = np.frombuffer(d.data, dtype=np.uint16).reshape(d.height, d.width).astype(np.float32)/1000.0
c = img[d.height//3:2*d.height//3, d.width//3:2*d.width//3]
v = c[(c>0.15)&(c<10.0)]
depth = float(np.median(v)) if v.size else float("nan")
o = rospy.wait_for_message("/odom", Odometry, timeout=8); pos = o.pose.pose.position
i = rospy.wait_for_message("/imu", Imu, timeout=8); q = i.orientation
yaw = math.degrees(math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z)))
m = rospy.wait_for_message("/scan", LaserScan, timeout=8)
def mn(lo,hi):
    b=float("inf")
    for k,r in enumerate(m.ranges):
        if r!=r or r==float("inf") or r<=m.range_min: continue
        a=math.degrees(m.angle_min+k*m.angle_increment)
        if lo<=a<=hi and r<b: b=r
    return b
print("  深度相机中央距离=%.3f m | 雷达前±25°=%.2f m | odom x=%.3f y=%.3f | IMU yaw=%+.2f°"
      % (depth, mn(-25,25), pos.x, pos.y, yaw))
