/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  eslint: {
    // Disable ESLint during builds for deployment
    ignoreDuringBuilds: true,
  },
  typescript: {
    // Disable type checking during builds for deployment
    ignoreBuildErrors: true,
  },
  // Configure external image domains and image optimization
  images: {
    domains: [
      'maps.googleapis.com',
      'vacation-planner-backend-pamf46bhfa-uc.a.run.app',
      'localhost'
    ],
    // Disable image optimization in development to prevent NS_BINDING_ABORTED errors
    unoptimized: process.env.NODE_ENV === 'development',
  },
  // Server external packages for better build performance
  serverExternalPackages: [],
}

module.exports = nextConfig
