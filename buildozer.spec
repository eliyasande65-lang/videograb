[app]
title = VideoGrab
package.name = videograb
package.domain = org.videograb
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,xml
version = 1.0.0
requirements = python3,kivy==2.3.0,pyjnius,android,certifi,openssl,yt-dlp
orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/icon.png
presplash.filename = %(source.dir)s/icon.png
presplash.color = #1a1535
p4a.branch = v2024.01.21

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.manifest.intent_filters = intent_filters.xml

[buildozer]
log_level = 2
warn_on_root = 1
