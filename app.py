"""
Flask backend for Nifty Midcap Momentum Screener.
"""

import io
import os
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file

import screener as sc

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nifty-screener-secret-2024")

jobs = {}
jobs_lock = threading.Lock()


def run_job(job_id, file_bytes, holdings_bytes=None):
    def update_progress(msg):
        with jobs_lock:
            jobs[job_id]["progress"] = msg

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "running"

        result = sc.run_full_screen(
            file_bytes,
            holdings_bytes=holdings_bytes,
            progress_callback=update_progress,
        )

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = {
                "top10": result["top10"],
                "all_passed": result["all_passed"],
                "rejected": result["rejected"],
                "exit_signals": result["exit_signals"],
                "rotation_review": result.get(
                    "rotation_review",
                    {
                        "reviews": [],
                        "candidate_pool_size": 0,
                        "replacement_count": 0,
                        "cash_count": 0,
                    },
                ),
                "stats": result["stats"],
                "run_date": result["run_date"],
                "has_excel": result["excel_bytes"] is not None,
            }
            jobs[job_id]["excel_bytes"] = result["excel_bytes"]
            jobs[job_id]["progress"] = "Complete"

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["progress"] = f"Error: {str(e)}"
            jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    if "csv_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["csv_file"]

    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Please upload a .csv file"}), 400

    file_bytes = f.read()

    holdings_bytes = None

    if "holdings_file" in request.files:
        hf = request.files["holdings_file"]

        if hf and hf.filename and hf.filename.lower().endswith(".csv"):
            holdings_bytes = hf.read()

    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": "Starting…",
            "result": None,
            "excel_bytes": None,
            "error": None,
            "created_at": time.time(),
        }

    t = threading.Thread(
        target=run_job,
        args=(job_id, file_bytes, holdings_bytes),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "result": job["result"],
        "error": job.get("error"),
    })


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job["status"] != "done":
        return jsonify({"error": "Result not ready"}), 404

    if not job["excel_bytes"]:
        return jsonify({"error": "No Excel file generated"}), 404

    run_date = job["result"]["run_date"].replace(" ", "_")
    filename = f"Midcap_EMA_Screen_{run_date}.xlsx"

    return send_file(
        io.BytesIO(job["excel_bytes"]),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        as_attachment=True,
        download_name=filename,
    )


def cleanup_old_jobs():
    while True:
        time.sleep(3600)
        cutoff = time.time() - 7200

        with jobs_lock:
            old = [
                k for k, v in jobs.items()
                if v["created_at"] < cutoff
            ]

            for k in old:
                del jobs[k]


threading.Thread(
    target=cleanup_old_jobs,
    daemon=True,
).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
