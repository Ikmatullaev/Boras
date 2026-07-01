# Boras — Local credentials template
#
# Copy this file to config_local.py and fill in your real credentials:
#   cp config_local.example.py config_local.py
#
# config_local.py is gitignored — never commit real credentials.

CAMERA_IP = "10.0.0.1"          # your camera IP address
CAMERA_USER = "admin"           # camera ONVIF username
CAMERA_PASS = "your-camera-password"

# API_TOKEN is the password for the Boras web panel (HTTP Basic Auth).
# Username is "admin" by default (configurable via CRANE_AUTH_USERNAME env var).
# Choose a long random string — this is NOT the camera password.
API_TOKEN = "your-long-random-web-panel-password"
