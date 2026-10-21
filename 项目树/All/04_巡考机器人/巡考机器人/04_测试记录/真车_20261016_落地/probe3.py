# -*- coding: utf-8 -*-
from __future__ import print_function
import math, rospy
from sensor_msgs.msg import LaserScan, Imu
from nav_msgs.msg import Odometry
rospy.init_node("probe3", anonymous=True, disable_signals=True)
s = rospy.wait_for_message("/scan", LaserScan, timeout=8)
def mn(lo, hi):
    b = float("inf")
    for i, r in enumerate(s.ranges):
        if r != r or r == float("inf") or r <= s.range_min: continue
        a = math.degrees(s.angle_min + i*s.angle_increment)
        if lo <= a <= hi and r < b: b = r
    return b
def yaw_of(q):
    return math.degrees(math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z)))
i = rospy.wait_for_message("/imu", Imu, timeout=8)
o = rospy.wait_for_message("/odom", Odometry, timeout=8)
print("  前±25°=%.2f m | IMU yaw=%+.2f° | odom yaw=%+.2f°" % (mn(-25,25), yaw_of(i.orientation), yaw_of(o.pose.pose.orientation)))
