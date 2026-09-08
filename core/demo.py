# -*- coding: utf-8 -*-
"""演示数据：无真机 / adb 不可用时的完整替身，保证 UI 全流程可验证。

演示模式下所有"写操作"只模拟进度与结果，不产生真实副作用。
"""

DEMO_DEVICE = {
    "serial": "adb-R3CN10ABCDE-hG9kQ",
    "state": "device",
    "model": "Pixel 8 Pro",
    "device": "husky",
    "product": "husky",
    "transport": "usb",
    "transportId": "1",
}

DEMO_PROPS = {
    "ro.product.model": "Pixel 8 Pro",
    "ro.product.device": "husky",
    "ro.build.display.id": "AP2A.240605.024",
    "ro.build.version.release": "14",
    "ro.build.version.sdk": "34",
    "ro.build.version.codename": "REL",
    "ro.product.cpu.abi": "arm64-v8a",
    "ro.serialno": "R3CN10ABCDE",
    "ro.build.type": "user",
    "persist.sys.locale": "zh-CN",
}

# iOS 演示设备：serial 用 UDID 形态，平台判定靠 platform 字段（不靠猜）
DEMO_IOS_DEVICE = {
    "serial": "00008101-001A2B3C4D5E801E",
    "state": "device",
    "model": "iPhone 15 Pro",
    "device": "iPhone16,1",
    "product": "D83AP",
    "name": "iPhone（演示）",
    "iosVersion": "17.5.1",
    "transport": "usb",
    "transportId": "2",
    "platform": "ios",
}

# (名称, bundle id, 版本, 类型, 体积字节)
DEMO_IOS_APPS = [
    ("微信", "com.tencent.xin", "8.0.49", "user", 486145228),
    ("支付宝", "com.alipay.iphoneclient", "10.5.96", "user", 391774208),
    ("抖音", "com.ss.iphone.ugc.Aweme", "28.6.0", "user", 524288000),
    ("Chrome", "com.google.chrome.ios", "126.0.6478", "user", 312475648),
    ("淘宝", "com.taobao.taobao4iphone", "10.32.10", "user", 445644800),
    ("设置", "com.apple.Preferences", "17.5.1", "system", 62914560),
    ("相机", "com.apple.camera", "17.5.1", "system", 189399040),
    ("照片", "com.apple.mobileslideshow", "17.5.1", "system", 254803968),
    ("App Store", "com.apple.AppStore", "17.5.1", "system", 125829120),
    ("Safari", "com.apple.mobilesafari", "17.5.1", "system", 104857600),
]

DEMO_KERNEL = (
    "Linux version 6.1.0-android14-11-g8a2b1c3d4e5f "
    "(build-user@build-host) (Android (10087095, +pgo) clang version 17.0.2) #1 SMP PREEMPT"
)

DEMO_APPS = [
    ("微信", "com.tencent.mm", "8.0.49", "user", 486145228, True, 1234),
    ("Chrome", "com.android.chrome", "126.0.6478", "system", 312475648, True, 4521),
    ("Gmail", "com.google.android.gm", "2024.06.16", "system", 198180864, False, 0),
    ("抖音", "com.ss.android.ugc.aweme", "28.6.0", "third", 524288000, True, 3312),
    ("淘宝", "com.taobao.taobao", "10.32.10", "third", 445644800, False, 0),
    ("支付宝", "com.eg.android.AlipayGphone", "10.5.96", "third", 391774208, True, 2871),
    ("开发者调试工具", "com.example.sampleapp", "2.4.1", "third", 52428800, True, 2345),
    ("设置", "com.android.settings", "14", "system", 62914560, True, 1890),
    ("相机", "com.google.android.GoogleCamera", "9.2.114", "system", 289406976, False, 0),
    ("相册", "com.google.android.apps.photos", "6.88.0", "system", 254803968, True, 3980),
    ("地图", "com.google.android.apps.maps", "11.128.0", "system", 220200960, False, 0),
    ("YouTube", "com.google.android.youtube", "19.23.35", "system", 314572800, False, 0),
    ("网易云音乐", "com.netease.cloudmusic", "8.10.15", "third", 167772160, True, 4102),
    ("企业微信", "com.tencent.wework", "4.1.22", "third", 209715200, True, 3567),
    ("高德地图", "com.autonavi.minimap", "12.05.2", "third", 241172480, False, 0),
    ("QQ", "com.tencent.mobileqq", "8.9.88", "third", 367001600, True, 2987),
    ("哔哩哔哩", "tv.danmaku.bili", "7.72.0", "third", 209715200, False, 0),
    ("美团", "com.sankuai.meituan", "12.18.203", "third", 356515840, False, 0),
    ("京东", "com.jingdong.app.mall", "12.4.2", "third", 335544320, False, 0),
    ("百度网盘", "com.baidu.netdisk", "12.9.6", "third", 150994944, False, 0),
    ("WPS Office", "cn.wps.moffice_eng", "14.9.1", "third", 188743680, False, 0),
    ("系统界面", "com.android.systemui", "14", "system", 83886080, True, 1120),
    ("电话", "com.google.android.dialer", "14.0.2", "system", 67108864, True, 1620),
    ("短信", "com.google.android.apps.messaging", "messages_2024", "system", 58720256, False, 0),
]

DEMO_PERMS = [
    ("通讯录", "READ_CONTACTS", True),
    ("相机", "CAMERA", True),
    ("麦克风", "RECORD_AUDIO", True),
    ("定位(后台)", "ACCESS_BACKGROUND_LOCATION", False),
    ("存储空间", "READ_EXTERNAL_STORAGE", True),
    ("电话", "READ_PHONE_STATE", True),
    ("短信", "SEND_SMS", False),
    ("日历", "READ_CALENDAR", False),
    ("蓝牙", "BLUETOOTH_CONNECT", True),
    ("通知", "POST_NOTIFICATIONS", True),
    ("传感器", "BODY_SENSORS", False),
    ("定位(精确)", "ACCESS_FINE_LOCATION", True),
]

# 演示文件系统：path -> entries
def is_ios(serial=None):
    """演示模式下的平台判定：只看当前选中的是不是那台 iOS 演示机。"""
    return bool(serial) and serial == DEMO_IOS_DEVICE["serial"]


def demo_devices():
    return [dict(DEMO_DEVICE), dict(DEMO_IOS_DEVICE)]


def demo_detail(serial=None):
    if is_ios(serial):
        return {
            "serial": DEMO_IOS_DEVICE["serial"],
            "platform": "ios",
            "model": DEMO_IOS_DEVICE["model"],
            "device": DEMO_IOS_DEVICE["device"],
            "name": DEMO_IOS_DEVICE["name"],
            "iosVersion": DEMO_IOS_DEVICE["iosVersion"],
            "buildVersion": "21F90",
            "udid": DEMO_IOS_DEVICE["serial"],
            "serialNumber": "F2LX9K3QW0PL",
            "abi": "arm64e",
            "kernel": "",
            "rooted": False,
            "battery": {"level": 78, "status": "放电中", "powered": False, "charging": False},
            "storage": {"total": 256 * 1024 ** 3, "used": int(183.4 * 1024 ** 3),
                        "free": int(72.6 * 1024 ** 3), "ok": True},
            "state": "device",
            "props": {
                "DeviceName": DEMO_IOS_DEVICE["name"],
                "ProductType": DEMO_IOS_DEVICE["device"],
                "ProductVersion": DEMO_IOS_DEVICE["iosVersion"],
                "BuildVersion": "21F90",
                "UniqueDeviceID": DEMO_IOS_DEVICE["serial"],
                "CPUArchitecture": "arm64e",
            },
        }
    return {
        "serial": DEMO_DEVICE["serial"],
        "model": "Pixel 8 Pro",
        "device": "husky",
        "androidVersion": "14",
        "apiLevel": "34",
        "displayId": "AP2A.240605.024",
        "abi": "arm64-v8a",
        "kernel": DEMO_KERNEL,
        "rooted": False,
        "battery": {"level": 87, "status": "放电中", "powered": False, "charging": False},
        "state": "device",
        "props": DEMO_PROPS,
    }


def demo_packages(kind="all", serial=None):
    if is_ios(serial):
        return _ios_packages(kind)
    out = []
    for name, pkg, ver, typ, size, running, pid in DEMO_APPS:
        if kind == "third" and typ != "third":
            continue
        if kind == "system" and typ != "system":
            continue
        if kind == "disabled" and typ != "disabled":
            continue
        if kind == "user" and typ == "system":
            continue
        out.append(
            {
                "packageName": pkg,
                "label": name,
                "versionName": ver,
                "versionCode": "20240624",
                "type": typ,
                "sizeBytes": size,
                "running": running,
                "pid": pid,
                "enabled": True,
            }
        )
    return out


def _ios_packages(kind="all"):
    """iOS 演示应用：字段与 core.ios.Ios.list_packages 的输出保持一致。"""
    out = []
    for name, bid, ver, typ, size in DEMO_IOS_APPS:
        if kind == "third" and typ != "user":
            continue
        if kind == "system" and typ != "system":
            continue
        if kind == "user" and typ == "system":
            continue
        out.append(
            {
                "packageName": bid,
                "label": name,
                "versionName": ver,
                "versionCode": ver,
                "type": typ,
                "sizeBytes": size,
                "running": False,
                "pid": 0,
                "enabled": True,
                "platform": "ios",
            }
        )
    return out


def demo_app_detail(package, serial=None):
    if is_ios(serial):
        for name, bid, ver, typ, size in DEMO_IOS_APPS:
            if bid == package:
                break
        else:
            name, bid, ver, typ, size = ("未知应用", package, "1.0.0", "user", 10485760)
        return {
            "packageName": bid,
            "label": name,
            "versionName": ver,
            "versionCode": ver,
            "type": typ,
            "uid": "",
            "targetSdk": "",
            "running": False,
            "pid": 0,
            "enabled": True,
            # iOS 不给第三方读权限列表，演示数据也必须如实留空（前端会给出说明）
            "permissions": [],
            "granted": 0,
            "requested": 0,
            "permissionsUnsupported": True,
            "appSize": int(size * 0.6),
            "dataSize": int(size * 0.4),
            "cacheSize": 0,
            "sizeBytes": size,
            "codePath": "/private/var/containers/Bundle/Application/DEMO/" + bid + ".app",
            "dataDir": "/private/var/mobile/Containers/Data/Application/DEMO",
            "installer": "",
            "firstInstallTime": "",
            "lastUpdateTime": "",
        }
    base = None
    for name, pkg, ver, typ, size, running, pid in DEMO_APPS:
        if pkg == package:
            base = (name, pkg, ver, typ, size, running, pid)
            break
    if not base:
        base = ("未知应用", package, "1.0.0", "third", 10485760, False, 0)
    name, pkg, ver, typ, size, running, pid = base
    granted = sum(1 for p in DEMO_PERMS if p[2])
    return {
        "packageName": pkg,
        "label": name,
        "versionName": ver,
        "versionCode": "20240624",
        "type": typ,
        "uid": str(10100 + (abs(hash(pkg)) % 900)),
        "targetSdk": "34",
        "minSdk": "26",
        "running": running,
        "pid": pid,
        "enabled": True,
        "installer": "com.android.vending",
        "firstInstallTime": "2024-01-18 10:22:31",
        "lastUpdateTime": "2024-06-21 09:14:02",
        "sizeBytes": size,
        "appSize": int(size * 0.55),
        "dataSize": int(size * 0.3),
        "cacheSize": int(size * 0.15),
        "codePath": "/data/app/~~demo==/" + pkg + "-abc==/base.apk",
        "dataDir": "/data/user/0/" + pkg,
        "permissions": [
            {"name": lb, "full": "android.permission." + full, "granted": g}
            for lb, full, g in DEMO_PERMS
        ],
        "granted": granted,
        "requested": len(DEMO_PERMS),
    }
