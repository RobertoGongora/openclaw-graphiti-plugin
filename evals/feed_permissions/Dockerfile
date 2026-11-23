# Disposable image for canary.sh: rsync, POSIX ACL tools and setpriv, nothing else.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends rsync acl \
    && rm -rf /var/lib/apt/lists/*
COPY canary.sh /canary.sh
ENTRYPOINT ["sh", "/canary.sh"]
