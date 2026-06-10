# sf-tailscale-router — subnet router that advertises Fly's private IPv6 mesh
# to the Teraflop tailnet. ENG2-1234.
#
# Lets team members on the tailnet reach `<app>.internal` addresses for any
# Fly app in the teraflop org (sf-phoenix, sf-litellm, future apps) without
# rebuilding those apps' images.
FROM alpine:3.20

RUN apk add --no-cache \
      tailscale \
      socat \
      ca-certificates \
      iptables \
      ip6tables \
      iproute2

RUN mkdir -p /var/lib/tailscale /var/run/tailscale

COPY fly/router/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

ENTRYPOINT ["/usr/local/bin/start.sh"]
