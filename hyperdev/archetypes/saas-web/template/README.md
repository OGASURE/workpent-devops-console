SaaS Web Archetype (Flask + Gunicorn + systemd + nginx)

Placeholders:
- __APP_SLUG__ : folder/app id (e.g. tikaitik)
- __APP_NAME__ : display name
- __APP_PORT__ : internal localhost port (e.g. 9501)

Runtime:
- start.sh handles .env, venv, pip install, then gunicorn binds 127.0.0.1:PORT
- systemd runs start.sh
- nginx location snippet routes /__APP_SLUG__/ -> http://127.0.0.1:__APP_PORT__/
