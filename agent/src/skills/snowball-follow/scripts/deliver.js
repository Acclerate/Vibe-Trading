#!/usr/bin/env node

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

// --- CLI arg parsing ---
const args = process.argv.slice(2);
function getArg(name) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : null;
}
function hasFlag(name) {
  return args.includes(`--${name}`);
}

const configPath = getArg('config') || null;
const dryRun = hasFlag('dry-run');

// Read digest content
let digestContent = '';
if (getArg('message')) {
  digestContent = getArg('message');
} else if (getArg('file')) {
  digestContent = readFileSync(getArg('file'), 'utf-8');
} else {
  // Read from stdin
  for await (const chunk of process.stdin) {
    digestContent += chunk;
  }
}

if (!digestContent.trim()) {
  process.stderr.write('Error: No digest content provided\n');
  process.exit(1);
}

// --- Load config ---
let config = {};
if (configPath) {
  try {
    config = JSON.parse(readFileSync(configPath, 'utf-8'));
  } catch {}
}

const delivery = config.delivery || {};

// --- Dry run: output to stdout ---
if (dryRun || delivery.method === 'stdout') {
  process.stdout.write(digestContent);
  process.stderr.write('Dry run: no document created\n');
  process.exit(0);
}

// --- Feishu document delivery ---
if (delivery.method === 'feishu-doc') {
  try {
    const cliArgs = ['docs', '+create', '--title', '雪球投资日报', '--markdown', digestContent];

    if (delivery.folderToken) {
      cliArgs.push('--folder-token', delivery.folderToken);
    }

    process.stderr.write('Creating Feishu document...\n');
    const result = execFileSync('lark-cli', cliArgs, {
      encoding: 'utf-8',
      timeout: 30_000,
      stdio: ['pipe', 'pipe', 'pipe'],
      maxBuffer: 10 * 1024 * 1024,
    });

    // Try to extract URL from output
    const urlMatch = result.match(/https:\/\/[^\s"<>]+feishu\.cn[^\s"<>]*/);
    const docUrl = urlMatch ? urlMatch[0] : result.trim();

    const output = {
      status: 'ok',
      method: 'feishu-doc',
      doc_url: docUrl,
      raw_output: result.trim(),
    };

    process.stdout.write(JSON.stringify(output, null, 2));
    process.stderr.write(`Document created: ${docUrl}\n`);
  } catch (err) {
    process.stderr.write(`Error creating Feishu document: ${err.message}\n`);
    if (err.stderr) process.stderr.write(err.stderr);

    const output = {
      status: 'error',
      method: 'feishu-doc',
      error: err.message,
    };
    process.stdout.write(JSON.stringify(output, null, 2));
    process.exit(1);
  }
} else {
  process.stderr.write(`Unknown delivery method: ${delivery.method}\n`);
  process.stdout.write(digestContent);
  process.exit(0);
}
