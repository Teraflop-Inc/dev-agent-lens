#!/usr/bin/env bun

import { randomBytes, createHash } from 'node:crypto';
import { writeFile, readFile, unlink } from 'node:fs/promises';

// Constants
const OAUTH_AUTHORIZE_URL = 'https://claude.ai/oauth/authorize';
const OAUTH_TOKEN_URL = 'https://console.anthropic.com/v1/oauth/token';
const CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
const REDIRECT_URI = 'https://console.anthropic.com/oauth/code/callback';
const STATE_FILE = 'claude_oauth_state.json';
const CREDENTIALS_FILE = 'credentials.json';

// Types
interface StateData {
  state: string;
  code_verifier: string;
  timestamp: number;
  expires_at: number;
}

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  scope?: string;
}

interface ClaudeOAuthCredentials {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  scopes: string[];
  isMax: boolean;
}

interface CredentialsFile {
  claudeAiOauth: ClaudeOAuthCredentials;
}

/**
 * Saves OAuth state data to a JSON file for later verification
 */
async function saveState(state: string, codeVerifier: string): Promise<void> {
  const stateData: StateData = {
    state,
    code_verifier: codeVerifier,
    timestamp: Math.floor(Date.now() / 1000),
    expires_at: Math.floor(Date.now() / 1000) + 600 // 10 minutes
  };

  try {
    await writeFile(STATE_FILE, JSON.stringify(stateData, null, 2));
  } catch (error) {
    console.warn(`Warning: Could not save state file: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/**
 * Generates an OAuth login URL for Claude Code authentication
 */
export async function generateLoginUrl(): Promise<string> {
  // Generate secure random values
  const state = randomBytes(32).toString('hex');
  const codeVerifier = randomBytes(32).toString('base64url');
  
  // Create PKCE code challenge
  const codeChallenge = createHash('sha256')
    .update(codeVerifier)
    .digest('base64url');

  // Save state and code verifier for verification later
  await saveState(state, codeVerifier);

  // Build OAuth URL
  const params = new URLSearchParams({
    code: 'true',
    client_id: CLIENT_ID,
    response_type: 'code',
    redirect_uri: REDIRECT_URI,
    scope: 'org:create_api_key user:profile user:inference',
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
    state
  });

  const url = `${OAUTH_AUTHORIZE_URL}?${params.toString()}`;
  
  console.log(url);
  
  return url;
}

/**
 * Loads previously saved OAuth state from file
 */
export async function loadState(): Promise<StateData | null> {
  try {
    const data = await readFile(STATE_FILE, 'utf-8');
    return JSON.parse(data);
  } catch (error) {
    return null;
  }
}

/**
 * Verifies that the saved state is still valid (not expired)
 */
export async function verifyState(): Promise<boolean> {
  const stateData = await loadState();
  if (!stateData) {
    console.error('Error: No state file found. Please run the login process again.');
    return false;
  }

  const currentTime = Math.floor(Date.now() / 1000);
  if (currentTime > stateData.expires_at) {
    console.error('Error: State has expired (older than 10 minutes)');
    return false;
  }

  return true;
}

/**
 * Exchanges authorization code for access and refresh tokens
 */
export async function exchangeCode(authorizationCode: string): Promise<ClaudeOAuthCredentials | null> {
  // Clean up the authorization code in case it has URL fragments
  const cleanedCode = authorizationCode.split('#')[0]?.split('&')[0] ?? authorizationCode;
  
  // Load state to get code_verifier
  const stateData = await loadState();
  if (!stateData) {
    console.error('Error: Could not load state data');
    return null;
  }

  const params = {
    grant_type: 'authorization_code',
    client_id: CLIENT_ID,
    code: cleanedCode,
    redirect_uri: REDIRECT_URI,
    code_verifier: stateData.code_verifier,
    state: stateData.state
  };

  try {
    const response = await fetch(OAUTH_TOKEN_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://claude.ai/',
        'Origin': 'https://claude.ai'
      },
      body: JSON.stringify(params)
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`Error response: ${response.status} - ${errorText}`);
      return null;
    }

    const data = await response.json() as TokenResponse;
    
    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      expiresAt: (Math.floor(Date.now() / 1000) + data.expires_in) * 1000,
      scopes: data.scope ? data.scope.split(' ') : ['user:inference', 'user:profile'],
      isMax: true
    };
  } catch (error) {
    console.error(`Error making token request: ${error instanceof Error ? error.message : String(error)}`);
    return null;
  }
}

/**
 * Saves OAuth credentials to a JSON file
 */
export async function saveCredentials(tokens: ClaudeOAuthCredentials): Promise<boolean> {
  const credentials: CredentialsFile = {
    claudeAiOauth: tokens
  };

  try {
    await writeFile(CREDENTIALS_FILE, JSON.stringify(credentials, null, 2));
    return true;
  } catch (error) {
    console.error(`Error saving credentials: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}

/**
 * Removes the state file after successful authentication
 */
export async function cleanupState(): Promise<void> {
  try {
    await unlink(STATE_FILE);
  } catch (error) {
    console.warn(`Warning: Could not clean up state file: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/**
 * Reads credentials file and returns all the credential info as JSON
 */
async function getCredentials(): Promise<void> {
  try {
    const data = await readFile(CREDENTIALS_FILE, 'utf-8');
    const credentials: CredentialsFile = JSON.parse(data);
    
    const result = {
      accessToken: credentials.claudeAiOauth.accessToken,
      refreshToken: credentials.claudeAiOauth.refreshToken,
      expiresAt: credentials.claudeAiOauth.expiresAt,
      scopes: credentials.claudeAiOauth.scopes,
      isMax: credentials.claudeAiOauth.isMax
    };
    
    console.log(JSON.stringify(result, null, 2));
  } catch (error) {
    console.error(`Error reading credentials: ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  }
}

/**
 * Prompts user for input from stdin
 */
async function promptForInput(question: string): Promise<string> {
  process.stdout.write(question);
  const input = await new Promise<string>((resolve) => {
    process.stdin.once('data', (data) => {
      resolve(data.toString().trim());
    });
  });
  return input;
}

// CLI handling
if (import.meta.main) {
  const args = process.argv.slice(2);
  
  if (args.includes('--help') || args.includes('-h')) {
    console.log(`Usage: ${process.argv[1]} [authorization_code|--token]`);
    console.log('');
    console.log('  Without arguments: Generates an OAuth login URL and prompts for authorization code');
    console.log('  With authorization_code: Completes OAuth login and exchanges code for tokens');
    console.log('  With --token: Returns all credentials from saved credentials');
    console.log('');
    console.log('Arguments:');
    console.log('  authorization_code  The code received from the OAuth callback');
    console.log('');
    console.log('Options:');
    console.log('  --token             Return all credentials as JSON');
    console.log('  --help, -h          Show this help message');
    process.exit(0);
  }

  if (args.includes('--token')) {
    // Return all credentials
    await getCredentials();
    process.exit(0);
  }

  const authorizationCode = args[0];

  if (authorizationCode) {
    // Code exchange flow with provided code
    if (!await verifyState()) {
      console.error('Error: Invalid or expired state. Please run the login process again.');
      process.exit(1);
    }
    
    console.log('Exchanging authorization code for tokens...');

    const tokens = await exchangeCode(authorizationCode);
    
    if (tokens) {
      console.log('\nOAuth token exchange successful!');
      console.log(`Received scopes: ${tokens.scopes.join(', ')}`);
      
      // Save OAuth credentials
      if (await saveCredentials(tokens)) {
        await cleanupState();
        
        console.log('\n=== SUCCESS ===');
        console.log('OAuth login successful!');
        console.log(`Credentials saved to: ${CREDENTIALS_FILE}`);
        console.log(`Token expires at: ${new Date(tokens.expiresAt).toLocaleString()}`);
        console.log('===============');
        
        // Also output all credentials for convenience
        console.log('\nCredentials (JSON):');
        console.log(JSON.stringify({
          accessToken: tokens.accessToken,
          refreshToken: tokens.refreshToken,
          expiresAt: tokens.expiresAt,
          scopes: tokens.scopes,
          isMax: tokens.isMax
        }, null, 2));
        
        process.exit(0);
      }
    }
    
    console.error('Login failed!');
    process.exit(1);
  } else {
    // Interactive flow - generate URL and prompt for code
    console.log('Generating OAuth login URL...\n');
    await generateLoginUrl();
    console.log('\n1. Open the above URL in your browser');
    console.log('2. Complete the OAuth flow');
    console.log('3. Copy the authorization code from the callback URL\n');
    
    const code = await promptForInput('Enter the authorization code: ');
    
    if (!code) {
      console.error('No authorization code provided');
      process.exit(1);
    }
    
    // Verify state and exchange code
    if (!await verifyState()) {
      console.error('Error: Invalid or expired state. Please run the login process again.');
      process.exit(1);
    }
    
    console.log('\nExchanging authorization code for tokens...');

    const tokens = await exchangeCode(code);
    
    if (tokens) {
      console.log('\nOAuth token exchange successful!');
      console.log(`Received scopes: ${tokens.scopes.join(', ')}`);
      
      // Save OAuth credentials
      if (await saveCredentials(tokens)) {
        await cleanupState();
        
        console.log('\n=== SUCCESS ===');
        console.log('OAuth login successful!');
        console.log(`Credentials saved to: ${CREDENTIALS_FILE}`);
        console.log(`Token expires at: ${new Date(tokens.expiresAt).toLocaleString()}`);
        console.log('===============');
        
        // Also output all credentials for convenience
        console.log('\nCredentials (JSON):');
        console.log(JSON.stringify({
          accessToken: tokens.accessToken,
          refreshToken: tokens.refreshToken,
          expiresAt: tokens.expiresAt,
          scopes: tokens.scopes,
          isMax: tokens.isMax
        }, null, 2));
        
        process.exit(0);
      }
    }
    
    console.error('Login failed!');
    process.exit(1);
  }
}