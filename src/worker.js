export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/api/chargers") {
      const data = await env.EV_KV.get("chargers");
      if (!data) {
        return new Response(
          JSON.stringify({ error: "No data yet. The ingestion job has not run." }),
          { status: 503, headers: { "content-type": "application/json" } }
        );
      }
      return new Response(data, {
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "public, max-age=120",
        },
      });
    }

    return env.ASSETS.fetch(request);
  },
};
