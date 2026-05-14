FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html imprint.html terms.html privacy.html style.css /usr/share/nginx/html/
COPY og.png /usr/share/nginx/html/og.png
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
