FROM node:22.14.0-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts && npm cache clean --force
ENV NODE_ENV=production
# The run binds its own authorized source at /app and an independent read-only
# per-run named dependency volume at /app/node_modules. No parent repository mounts.
CMD ["node", "scripts/serve.mjs"]
