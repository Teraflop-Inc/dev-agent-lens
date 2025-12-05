# AMD64 Multi-Architecture Support

This directory contains AMD64/x86_64 support for the LiteLLM OAuth fix, enabling dev-agent-lens to run on standard Intel/AMD servers in addition to ARM-based systems (M1/M2 Macs, ARM cloud instances).

## Problem

The original `aowen14/litellm-oauth-fix:latest` image only supports ARM64 architecture, which prevents deployment on:
- Standard x86_64 cloud VMs (AWS EC2, GCP, Azure, DigitalOcean)
- Intel/AMD desktop/laptop machines
- Most CI/CD runners (GitHub Actions, GitLab CI, CircleCI)
- Traditional data center infrastructure

## Solution

We've created AMD64-specific versions of the OAuth fix files that maintain 100% functional compatibility with the ARM version:

### Files Added

1. **`Dockerfile.amd64`** - Dockerfile optimized for x86_64 architecture
2. **`amd64_litellm_pre_call_utils.py`** - OAuth token extraction (AMD64)
3. **`amd64_anthropic_passthrough_logging_handler.py`** - Logging handler (AMD64)
4. **`common_utils.py`** - Shared utilities (architecture-agnostic)

### Key Features

- **Dual-path installation**: Installs OAuth fix in both `/app/litellm/proxy/` and Python site-packages
- **Dynamic path detection**: Uses `sysconfig` to find the correct site-packages location
- **Verification step**: Confirms OAuth code is present in both locations
- **Drop-in replacement**: Uses same configuration files as ARM version

## Building

### AMD64 Image

```bash
docker build -f Dockerfile.amd64 -t litellm-oauth-fix:amd64 .
```

### Multi-Architecture (requires Docker Buildx)

```bash
# Create and use multi-arch builder
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap

# Build for both architectures
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t yourusername/litellm-oauth-fix:latest \
  --push \
  .
```

## Usage

### Update docker-compose.yml

For AMD64 deployments, update the image reference:

```yaml
services:
  litellm-proxy:
    # Before (ARM only)
    # image: aowen14/litellm-oauth-fix:latest

    # After (AMD64)
    image: litellm-oauth-fix:amd64
    # OR use multi-arch image
    # image: yourusername/litellm-oauth-fix:latest
```

Then start normally:

```bash
docker compose --profile phoenix up -d
```

## Verification

Check that OAuth fix is installed correctly:

```bash
# Get container name
CONTAINER=$(docker ps --filter "name=litellm" --format "{{.Names}}" | head -1)

# Check line counts (should be same in both locations)
docker exec $CONTAINER wc -l /app/litellm/proxy/litellm_pre_call_utils.py
docker exec $CONTAINER python -c "import sysconfig; import os; path = os.path.join(sysconfig.get_paths()['purelib'], 'litellm/proxy/litellm_pre_call_utils.py'); print(path)" | xargs docker exec $CONTAINER wc -l

# Verify OAuth detection code is present (should find multiple matches)
docker exec $CONTAINER grep -c "sk-ant-oat" /app/litellm/proxy/litellm_pre_call_utils.py
```

## Testing

Test with Claude Code:

```bash
# Start AMD64 stack
docker compose --profile phoenix up -d

# Use claude-lens wrapper
./claude-lens

# Or configure Claude Code directly
export ANTHROPIC_BASE_URL=http://localhost:4000
codex "test OAuth passthrough"
```

Check logs for OAuth token detection:

```bash
docker logs litellm-proxy-phoenix 2>&1 | grep -i "oauth"
```

## Architecture Comparison

| Feature | ARM64 | AMD64 |
|---------|-------|-------|
| OAuth Passthrough | ✅ | ✅ |
| Token Extraction | ✅ | ✅ |
| OpenTelemetry | ✅ | ✅ |
| Phoenix Integration | ✅ | ✅ |
| Arize Integration | ✅ | ✅ |
| Configuration Files | Same | Same |
| Performance | Native ARM | Native x86 |

## Maintenance

Both ARM and AMD64 versions should be kept in sync. When updating OAuth fix logic:

1. Update `arm_*.py` files for ARM64
2. Update `amd64_*.py` files for AMD64
3. Rebuild both images
4. Test on both architectures

## Contributing

To contribute AMD64 improvements:

1. Fork this repository
2. Create a feature branch
3. Make changes to `amd64_*` files
4. Test on x86_64 hardware
5. Submit PR with test results

## Credits

- Original ARM OAuth fix: [@aowen14](https://github.com/aowen14)
- AMD64 port: AusterIT Platform team
- Based on [Teraflop-Inc/dev-agent-lens](https://github.com/Teraflop-Inc/dev-agent-lens)
