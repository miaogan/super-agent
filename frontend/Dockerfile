# 多阶段构建：node 构建产物 + nginx 托管
FROM node:20-alpine AS builder
WORKDIR /app
# 构建参数：VITE_API_URL（跨源部署填后端地址；同源留空走 /api 相对路径）
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
# 模板文件：容器启动时用 envsubst 替换 ${NGINX_BACKEND_URL} 等环境变量
COPY nginx.conf /etc/nginx/templates/default.conf.template
# 默认后端地址（同 docker compose 网络内容器名）
ENV NGINX_BACKEND_URL=http://backend:8000
EXPOSE 80
# nginx:alpine 自带 envsubst，启动时替换模板后加载
CMD ["nginx", "-g", "daemon off;"]
