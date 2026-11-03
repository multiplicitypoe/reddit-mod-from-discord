import type {IncomingMessage, ServerResponse} from 'node:http'
import {createServer, getServerPort} from '@devvit/web/server'
import {onCommentDelete, onCommentReport, onPostDelete, onPostReport} from './reports.ts'

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = []
  for await (const chunk of req) chunks.push(chunk as Buffer)
  const text = Buffer.concat(chunks).toString('utf8')
  return text ? JSON.parse(text) : {}
}

function send(res: ServerResponse, status: number, body: unknown): void {
  // The Devvit trigger wrapper rejects responses without a Content-Length.
  const text = JSON.stringify(body)
  res.writeHead(status, {'content-type': 'application/json', 'content-length': Buffer.byteLength(text)})
  res.end(text)
}

const server = createServer(async (req, res) => {
  try {
    if (req.method !== 'POST') return send(res, 404, {error: 'not found'})
    switch (req.url) {
      case '/internal/triggers/on-post-report': {
        const result = await onPostReport((await readJson(req)) as Parameters<typeof onPostReport>[0])
        console.log(`post-report; result=${result}`)
        return send(res, 200, {status: result})
      }
      case '/internal/triggers/on-comment-report': {
        const result = await onCommentReport((await readJson(req)) as Parameters<typeof onCommentReport>[0])
        console.log(`comment-report; result=${result}`)
        return send(res, 200, {status: result})
      }
      case '/internal/triggers/on-post-delete': {
        const result = await onPostDelete((await readJson(req)) as Parameters<typeof onPostDelete>[0])
        console.log(`post-delete; result=${result}`)
        return send(res, 200, {status: result})
      }
      case '/internal/triggers/on-comment-delete': {
        const result = await onCommentDelete((await readJson(req)) as Parameters<typeof onCommentDelete>[0])
        console.log(`comment-delete; result=${result}`)
        return send(res, 200, {status: result})
      }
      default:
        return send(res, 404, {error: 'not found'})
    }
  } catch (err) {
    console.error(`server error; ${err instanceof Error ? err.stack : err}`)
    send(res, 500, {error: 'internal error'})
  }
})

server.on('error', err => console.error(`server error; ${err.stack}`))
server.listen(getServerPort())
