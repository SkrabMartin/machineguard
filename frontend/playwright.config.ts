import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './browser',
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:4173' },
  webServer: { command: 'npm run dev -- --port 4173 --strictPort', url: 'http://127.0.0.1:4173', reuseExistingServer: false, timeout: 30000 },
  retries: 0,
})
