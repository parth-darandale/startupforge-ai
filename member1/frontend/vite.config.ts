import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const localPath = (relativePath: string) => decodeURIComponent(new URL(relativePath, import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, '$1')
const reactPackage = localPath('./node_modules/react')
const reactDomPackage = localPath('./node_modules/react-dom')

export default defineConfig({
  plugins: [react()],
  resolve: { alias: [{ find: 'react', replacement: reactPackage }, { find: 'react-dom', replacement: reactDomPackage }] },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
