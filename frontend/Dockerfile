# 生产前端镜像（stage 6C spec S）：multi-stage Node → build → nginx。
#
# 运行时只托管静态 dist + nginx 同源反代 /api 到后端容器（避免 CORS）。
# VITE_API_BASE_URL 在构建期注入（同源 /api/v1）；`backend` 是 compose 网络内
# 的服务名（见仓库根 docker-compose.yml）。
FROM m.daocloud.io/docker.io/library/node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com && npm ci

COPY . .
# 同源：浏览器请求 /api/v1 → nginx 反代到后端容器 8001。
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM m.daocloud.io/docker.io/library/nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
