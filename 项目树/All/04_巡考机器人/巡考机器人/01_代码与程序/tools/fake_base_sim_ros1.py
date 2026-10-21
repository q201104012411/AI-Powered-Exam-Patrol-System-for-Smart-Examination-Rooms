#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fake_base_sim_ros1.py —— ROS1 版"假底盘"仿真器，用来在不动电机的前提下验证 patrol_move_ros1.py

发布：/odom (nav_msgs/Odometry)、/scan (sensor_msgs/LaserScan)、/PowerVoltage (std_msgs/Float32)
订阅：/cmd_vel (geometry_msgs/Twist)

用法（在小车上，先启动 roscore）：
  export ROS_MASTER_URI=http://localhost:11311
  roscore &                                   # 或另开终端 roscore
  python fake_base_sim_ros1.py --akm           # 模拟阿克曼：v=0 时不给偏航（不能原地转圈）
  python fake_base_sim_ros1.py --akm --obstacle-start 5 --obstacle-end 12
                                               # 第 5~12 秒前方 0.2m 有障碍，测避障停车
  python fake_base_sim_ros1.py                 # 模拟差速：允许原地旋转
兼容 python2.7 / python3。
"""
from __future__ import print_function

import argparse
import math
import random
import time

import rospy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Float32


def yaw_to_quat(yaw):
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


class FakeBase(object):
    def __init__(self, args):
        self.args = args
        self.x = self.y = self.yaw = 0.0
        self.vx = self.vy = self.wz = 0.0
        self.v = 11.9
        self.t0 = time.time()

        self.odom_pub = rospy.Publisher(args.odom_topic, Odometry, queue_size=10)
        self.scan_pub = rospy.Publisher(args.scan_topic, LaserScan, queue_size=5)
        self.volt_pub = rospy.Publisher(args.voltage_topic, Float32, queue_size=5)
        rospy.Subscriber(args.cmd_topic, Twist, self.on_cmd, queue_size=10)
        rospy.loginfo("fake_base_sim_ros1 started: %s -> %s / %s (akm=%s)"
                      % (args.cmd_topic, args.odom_topic, args.scan_topic, args.akm))

    def on_cmd(self, m):
        self.vx, self.vy, self.wz = m.linear.x, m.linear.y, m.angular.z

    def obstacle_now(self):
        if self.args.obstacle_start is None:
            return False
        t = time.time() - self.t0
        return self.args.obstacle_start <= t <= self.args.obstacle_end

    def blocked_at(self, raw_angle):
        """raw_angle 是雷达自身坐标系的角；按安装朝向换算到车体角度再判断。
        Wheeltec 雷达倒装（base_to_laser yaw=3.14）时应传 --lidar-yaw-deg 180。"""
        body = raw_angle + math.radians(self.args.lidar_yaw_deg)
        while body > math.pi:
            body -= 2.0 * math.pi
        while body < -math.pi:
            body += 2.0 * math.pi
        return self.obstacle_now() and abs(body) < math.radians(self.args.obstacle_arc)

    def spin(self):
        rate = rospy.Rate(self.args.rate)
        last = time.time()
        n = 360
        while not rospy.is_shutdown():
            now = time.time()
            dt = min(0.1, now - last)
            last = now

            wz = self.wz
            if self.args.akm and abs(self.vx) < 0.005:
                wz = 0.0            # 阿克曼：停着打方向盘不会改变航向
            self.yaw += wz * dt
            self.x += (self.vx * math.cos(self.yaw) - self.vy * math.sin(self.yaw)) * dt
            self.y += (self.vx * math.sin(self.yaw) + self.vy * math.cos(self.yaw)) * dt
            if self.args.noise > 0:
                self.x += random.gauss(0, self.args.noise * dt)
                self.y += random.gauss(0, self.args.noise * dt)

            m = Odometry()
            m.header.stamp = rospy.Time.now()
            m.header.frame_id = "odom"
            m.child_frame_id = "base_footprint"
            m.pose.pose.position.x = self.x
            m.pose.pose.position.y = self.y
            qx, qy, qz, qw = yaw_to_quat(self.yaw)
            m.pose.pose.orientation.x, m.pose.pose.orientation.y = qx, qy
            m.pose.pose.orientation.z, m.pose.pose.orientation.w = qz, qw
            m.twist.twist.linear.x = self.vx
            m.twist.twist.linear.y = self.vy
            m.twist.twist.angular.z = self.wz
            self.odom_pub.publish(m)

            if self.args.publish_scan:
                s = LaserScan()
                s.header.stamp = m.header.stamp
                s.header.frame_id = "laser"
                s.angle_min = -math.pi
                s.angle_max = math.pi
                s.angle_increment = 2.0 * math.pi / n
                s.range_min = 0.05
                s.range_max = 12.0
                ranges = []
                for i in range(n):
                    a = s.angle_min + i * s.angle_increment
                    if self.blocked_at(a):
                        ranges.append(self.args.obstacle_range)
                    else:
                        ranges.append(5.0)
                s.ranges = ranges
                self.scan_pub.publish(s)

            self.v -= 0.0002
            self.volt_pub.publish(Float32(data=self.v))
            rate.sleep()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cmd-topic", default="/cmd_vel")
    p.add_argument("--odom-topic", default="/odom")
    p.add_argument("--scan-topic", default="/scan")
    p.add_argument("--voltage-topic", default="/PowerVoltage")
    p.add_argument("--akm", action="store_true", help="模拟阿克曼：v=0 时不能改变航向")
    p.add_argument("--rate", type=float, default=50.0)
    p.add_argument("--no-scan", dest="publish_scan", action="store_false", default=True)
    p.add_argument("--obstacle-start", type=float, default=None)
    p.add_argument("--obstacle-end", type=float, default=1e9)
    p.add_argument("--obstacle-range", type=float, default=0.20)
    p.add_argument("--obstacle-arc", type=float, default=20.0)
    p.add_argument("--lidar-yaw-deg", type=float, default=0.0,
                   help="模拟雷达安装朝向：Wheeltec 雷达倒装填 180（障碍物放在车头正前方时，"
                        "原始扫描角出现在 ±180°）")
    p.add_argument("--noise", type=float, default=0.0)
    args = p.parse_args()

    rospy.init_node("fake_base_sim", anonymous=True, disable_signals=False)
    FakeBase(args).spin()


if __name__ == "__main__":
    main()
