FROM victoriametrics/victoria-metrics:v1.154.0

RUN apk --update --no-cache add curl su-exec python3 py3-pip py3-aiohttp

COPY load.sh /usr/local/bin/omnistat-load
COPY index.py /usr/local/bin/omnistat-index
COPY entrypoint.sh /usr/local/bin/entrypoint

RUN chmod 0777 /usr/local/bin/omnistat-load
RUN chmod 0777 /usr/local/bin/omnistat-index

ENTRYPOINT ["/usr/local/bin/entrypoint"]
