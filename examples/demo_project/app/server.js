const http = require("node:http");

function handleRequest(req, res) {
  const payload = JSON.parse(req.body);
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ ok: true, order: payload.order.id }));
}

const server = http.createServer(handleRequest);
server.listen(3000, () => console.log("demo gateway listening on :3000"));
