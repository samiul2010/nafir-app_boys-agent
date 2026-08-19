[app]
title = VoiceCrew
package.name = voicecrew
package.domain = com.voicecrew
source.dir = .
source.include_exts = py,kv,txt,md,json
version = 0.4.0
# requests 2.25.1 uses the pure-Python chardet path and avoids the unsupported
# charset-normalizer cp314 Android wheel selected by newer requests releases.
requirements = python3,kivy==2.3.1,requests==2.25.1,pypdf,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
# Python 3.14 uses preadv/pwritev, which require Android API 24 or newer.
android.minapi = 24
# p4a develop/libthorvg requires the current NDK layout for libomp.so.
android.ndk = 28c
android.ndk_api = 24
android.archs = armeabi-v7a, arm64-v8a

# Use the upstream p4a fix for stale/corrupted build venv pip files.
p4a.branch = develop
p4a.commit = 0382d27de2f7315ed98e74884bafb30365decdee
android.private_storage = True
android.accept_sdk_license = True
android.allow_backup = False
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 0
