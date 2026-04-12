import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 5,
  iterations: 25,
};

const BASE_URL = __ENV.ASTRIXA_BASE_URL || 'http://127.0.0.1:18080';
const TOKEN = __ENV.ASTRIXA_GATEWAY_TOKEN || 'astrixa-dev-token';
const MODEL = __ENV.ASTRIXA_MODEL || 'mock-1';

export default function () {
  const response = http.post(
    `${BASE_URL}/v1/chat/completions`,
    JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: 'load test hello from astrixa' }],
    }),
    {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        'Content-Type': 'application/json',
      },
    },
  );

  check(response, {
    'status is 200': (r) => r.status === 200,
  });

  sleep(0.2);
}

