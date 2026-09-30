serve:
    podman run \
        -p 3000:8080 \
        -v ./data:/app/backend/data:Z \
        -d ghcr.io/open-webui/open-webui@sha256:1a6399d237dc392a2313e0ca826020b3fd5d22536357840eb63393d18dc8b924

# Delete conversations whose last message is older than 3 days (run once).
cleanup *ARGS:
    python3 cleanup.py {{ARGS}}

# Keep running and repeat the cleanup every MINUTES.
cleanup-every MINUTES="60":
    python3 cleanup.py --every {{MINUTES}}

test:
    python3 -m unittest -v
