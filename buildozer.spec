[app]
title = VoiceCrew
package.name = voicecrew
package.domain = com.voicecrew
source.dir = .
source.include_exts = py,kv,txt,md,json
version = 0.4.0
requirements = python3,kivy==2.3.1,requests,pypdf,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
# Python 3.14 uses preadv/pwritev, which require Android API 24 or newer.
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = armeabi-v7a, arm64-v8a
android.private_storage = True
android.accept_sdk_license = True
android.allow_backup = False
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 0
