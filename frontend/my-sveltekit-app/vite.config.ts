import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [
    // Guard against malformed URIs from bot probes crashing the SvelteKit dev server
    {
      name: 'malformed-uri-guard',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          try {
            decodeURI(req.url || '');
          } catch {
            // Invalid percent-encoding in URL — reject before SvelteKit sees it
            _res.statusCode = 400;
            _res.end('Bad Request');
            return;
          }
          next();
        });
      },
    },
    // Public dev server: bot scanners sweep /src/ and /@fs/ for credential
    // files (.env, .git-credentials, terraform.tfstate, nginx.conf, ...).
    // Vite treats anything under those prefixes as a module load, so a miss
    // becomes an "Internal server error" that is broadcast over HMR -- which
    // paints an error overlay in every connected browser, indistinguishable
    // from a real build failure. Nothing leaks (fs.allow already answers 403),
    // so this only stops the noise: answer probes with a plain 404 before the
    // transform middleware tries to open them.
    {
      name: 'config-probe-guard',
      configureServer(server) {
        // Scoped to the module-loader prefixes so real app routes never match.
        const LOADER_PREFIX = /^\/(?:src|@fs)\//;
        const PROBE = new RegExp(
          // credential dotfiles, by exact basename
          '(?:^|/)(?:\\.env(?:\\.[\\w.-]+)?|\\.netrc|\\.pypirc|\\.git-credentials'
          + '|\\.htpasswd|\\.dockercfg|id_rsa[\\w.]*'
          // credential JSON by exact name -- .json is NOT blanket-blocked,
          // because apps legitimately import data as .json
          + '|gcp-key\\.json|serviceAccountKey\\.json'
          + '|firebase-service-account\\.json|service-account\\.json)$'
          // config/state/secret extensions never imported by this app
          + '|\\.(?:ya?ml|conf|cnf|ini|properties|php|tfvars|tfstate|pem|key'
          + '|crt|p12|pfx|sql|bak|swp|old|log|py|prisma)$',
          'i',
        );
        server.middlewares.use((req, res, next) => {
          let pathname;
          try {
            pathname = new URL(req.url || '', 'http://localhost').pathname;
          } catch {
            return next(); // malformed-uri-guard above already handled these
          }
          if (LOADER_PREFIX.test(pathname) && PROBE.test(pathname)) {
            res.statusCode = 404;
            res.end('Not Found');
            return;
          }
          next();
        });
      },
    },
    sveltekit(),
  ],
  
  // Server configuration with improved WebSocket handling and API proxy
  server: {
    hmr: {
      overlay: true,
      // When accessed via nginx (HTTPS on 443), route HMR through nginx too
      clientPort: parseInt(process.env.HMR_CLIENT_PORT || '443'),
      protocol: process.env.HMR_PROTOCOL || 'wss',
    },
    port: 5173,
    strictPort: false,
    host: '0.0.0.0',
    allowedHosts: ['aicc.uksouth.cloudapp.azure.com'],
    // Add watch configuration to ignore node_modules
    watch: {
      ignored: ['**/node_modules/**', '**/dist/**', '**/.DS_Store']
    },
    // Proxy API calls to Django backend (development only)
    proxy: {
      '/api': {
        target: process.env.BACKEND_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => {
          console.log(`🔄 Proxying: ${path} -> ${process.env.BACKEND_URL || 'http://127.0.0.1:8000'}${path}`);
          return path;
        },
        configure: (proxy, options) => {
          proxy.on('error', (err, req, res) => {
            console.log('❌ Proxy Error:', err.message);
          });
          proxy.on('proxyReq', (proxyReq, req, res) => {
            console.log('📡 Proxy Request:', req.method, req.url);
          });
          proxy.on('proxyRes', (proxyRes, req, res) => {
            console.log('✅ Proxy Response:', proxyRes.statusCode, req.url);
          });
        }
      }
    }
  },
  
  // Updated optimizeDeps configuration with explicit exclusions
  optimizeDeps: {
    include: [
      'svelte',
      'svelte/store',
      'svelte/transition',
      'axios'
    ],
    exclude: [
      '@sveltejs/kit',
      '@sveltejs/kit/hooks',
      '@sveltejs/kit/node',
      '@sveltejs/kit/node/polyfills',
      '@sveltejs/kit/vite'
    ],
    esbuildOptions: {
      target: 'esnext'
    }
  },
  
  // Build configuration
  build: {
    reportCompressedSize: false,
    target: 'esnext',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1000
  },
  
  // Use a different cache directory
  cacheDir: '.vite_cache',
  
  // Resolve configuration
  resolve: {
    dedupe: ['svelte', 'svelte/transition', 'svelte/store']
  }
});
