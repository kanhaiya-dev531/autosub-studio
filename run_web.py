"""Start the website on your own computer:  python run_web.py"""

import os

from web import create_app

if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"AutoSub Studio is running at http://{host}:{port}  (press Ctrl+C to stop)")
    app.run(host=host, port=port, threaded=True, debug=os.environ.get("FLASK_DEBUG") == "1")
