#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fake_base_sim.py —— 无实车时的"假底盘"仿真器，用来验证 patrol_move.py

它订阅 /cmd_vel，按简单运动学积分出位姿，并发布：
  /odom          (nav_msgs/Odometry)           —— 供 patrol_move.py 闭环
  /scan          (sensor_msgs/LaserScan)       —— 可注入障碍物，测试避障停车
  /PowerVoltage  (std_msgs/Float32)            —— 模拟电池电压

用法：
  python3 tools/fake_base_sim.py                       # 前方永远通畅
  python3 tools/fake_base_sim.py --obstacle-start 5 --obstacle-end 12
                                                       # 第 5~12 秒前方 0.2m 有障碍
  python3 tools/fake_base_sim.py --noise 0.02          # 加里程计噪声
"""
import argparse
import math
import random
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Float32


def yaw_to_quat(yaw):
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


class FakeBase(Node):
    def __init__(self, args):
        super().__init__("fake_base_sim")
        self.args = args
        self.x = self.y = self.yaw = 0.0
        self.vx = self.vy = self.wz = 0.0
        self.v = 11.9
        self.t0 = time.monotonic()

        self.create_subscription(Twist, args.cmd_topic, self.on_cmd, 10)
        self.odom_pub = self.create_publisher(Odometry, args.odom_topic, 10)
        self.scan_pub = self.create_publisher(LaserScan, args.scan_topic, 10)
        self.volt_pub = self.create_publisher(Float32, args.voltage_topic, 10)

        self.last = time.monotonic()
        self.create_timer(0.02, self.step)      # 50Hz 积分
        self.create_timer(0.10, self.publish_scan)
        self.create_timer(1.00, self.publish_voltage)
        self.get_logger().info("fake_base_sim started: %s -> %s / %s" %
                               (args.cmd_topic, args.odom_topic, args.scan_topic))

    def on_cmd(self, msg):
        self.vx, self.vy, self.wz = msg.linear.x, msg.linear.y, msg.angular.z

    def step(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last)
        self.last = now
        # 一阶惯性，模拟加减速
        self.yaw += self.wz * dt
        self.x += (self.vx * math.cos(self.yaw) - self.vy * math.sin(self.yaw)) * dt
        self.y += (self.vx * math.sin(self.yaw) + self.vy * math.cos(self.yaw)) * dt
        if self.args.noise > 0:
            self.x += random.gauss(0, self.args.noise * dt)
            self.y += random.gauss(0, self.args.noise * dt)

        m = Odometry()
        m.header.stamp = self.get_clock().now().to_msg()
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

    def obstacle_now(self):
        if self.args.obstacle_start is None:
            return False
        t = time.monotonic() - self.t0
        return self.args.obstacle_start <= t <= self.args.obstacle_end

    def blocked_at(self, raw_angle):
        body = raw_angle + math.radians(self.args.lidar_yaw_deg)
        body = (body + math.pi) % (2.0 * math.pi) - math.pi
        return self.obstacle_now() and abs(body) < math.radians(self.args.obstacle_arc)

    def publish_scan(self):
        n = 360
        m = LaserScan()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = "laser"
        m.angle_min = -math.pi
        m.angle_max = math.pi
        m.angle_increment = 2.0 * math.pi / n
        m.range_min = 0.05
        m.range_max = 12.0
        r = []
        for i in range(n):
            a = m.angle_min + i * m.angle_increment
            if self.blocked_at(a):
                r.append(self.args.obstacle_range)
            else:
                r.append(5.0)
        m.ranges = r
        self.scan_pub.publish(m)

    def publish_voltage(self):
        self.v -= 0.0005
        self.volt_pub.publish(Float32(data=self.v))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cmd-topic", default="/cmd_vel")
    p.add_argument("--odom-topic", default="/odom")
    p.add_argument("--scan-topic", default="/scan")
    p.add_argument("--voltage-topic", default="/PowerVoltage")
    p.add_argument("--obstacle-start", type=float, default=None)
    p.add_argument("--obstacle-end", type=float, default=1e9)
    p.add_argument("--obstacle-range", type=float, default=0.20)
    p.add_argument("--obstacle-arc", type=float, default=20.0)
    p.add_argument("--lidar-yaw-deg", type=float, default=0.0,
                   help="模拟雷达安装朝向，倒装填 180")
    p.add_argument("--noise", type=float, default=0.0)
    args = p.parse_args()

    rclpy.init()
    node = FakeBase(args)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
