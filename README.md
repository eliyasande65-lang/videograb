# VideoGrab

Paste a video link, pick a quality, download. Kivy + yt-dlp, built to an Android APK (32-bit and 64-bit).

## Build the APK
1. Create a GitHub repo and upload everything in this folder (keep the `.github/workflows/build.yml` path).
2. Open the Actions tab and wait for "Build VideoGrab APK" (first build takes about 20-40 minutes).
3. Download the `videograb-apk` artifact, unzip it and install the APK.

## Test on a PC first (optional)
    pip install kivy yt-dlp certifi
    python main.py
Files are saved to ~/Downloads/VideoGrab.

## Notes
- Downloads are saved to Downloads/VideoGrab on the phone.
- If a site stops working, open More > Update engine.
- Share any link to VideoGrab from another app to load it automatically.
