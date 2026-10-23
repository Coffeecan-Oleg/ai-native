from starlette.responses import FileResponse, Response
from gateway_mcp.services import branding


def register_branding_routes(mcp):
    @mcp.custom_route('/branding/{asset:path}', methods=['GET'], include_in_schema=False)
    async def brand_asset(request):
        asset = request.path_params['asset']
        if not branding.enabled() or asset not in branding.ASSETS:
            return Response(status_code=404)
        return FileResponse(branding.ROOT / asset, media_type=branding.ASSETS[asset],
                            headers={'Cache-Control': 'public, max-age=3600',
                                     'X-Content-Type-Options': 'nosniff',
                                     'Content-Security-Policy': "default-src 'none'"})
