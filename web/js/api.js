export async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  if (response.status === 204) return null;
  return response.json();
}

export async function errorMessage(response) {
  let message = `${response.status} ${response.statusText}`;
  try {
    const data = await response.json();
    message = typeof data.detail === "string" ? data.detail : data.message || message;
  } catch (_) { }
  return message;
}

function parseBlock(block) {
  let event = "message";
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try { return { event, data: JSON.parse(data.join("\n")) }; }
  catch (_) { return { event, data: { text: data.join("\n") } }; }
}

export async function* readSSE(response) {
  if (!response.body) throw new Error("This browser does not support response streaming.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      for (const block of blocks) { const ev = parseBlock(block); if (ev) yield ev; }
    }
    const tail = parseBlock(buffer);
    if (tail) yield tail;
  } finally {
    reader.cancel().catch(() => {});
  }
}
