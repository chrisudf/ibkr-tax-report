"""gunicorn settings for the hosted container (Dockerfile CMD)."""

bind = "0.0.0.0:8000"

# One process on purpose: the rate limiter and the hot-swapped RBA table live
# in its memory. Threads cover concurrent requests; a report takes well under
# a second.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 60
graceful_timeout = 20

# The root filesystem is read-only; the heartbeat file goes to /dev/shm (RAM).
worker_tmp_dir = "/dev/shm"

# No access log: download URLs carry result tokens, and nothing needs the list
# of who fetched what. Errors (without request bodies) go to stderr.
accesslog = None
errorlog = "-"
loglevel = "info"

# gunicorn 25+ opens a control socket under $HOME by default; the app user has
# no home and the root filesystem is read-only, so it only logs an error.
control_socket_disable = True


def post_worker_init(worker):
    # start the result sweeper and RBA refresher in the serving process
    from app import start_background
    start_background()
