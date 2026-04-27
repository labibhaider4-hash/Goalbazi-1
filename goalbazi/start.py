import os

from server import app, app_port


if __name__ == "__main__":
    print(f"Starting Goalbazi on PORT={os.environ.get('PORT')!r}, resolved={app_port()}", flush=True)
    app.run(host="0.0.0.0", port=app_port(), debug=False)
