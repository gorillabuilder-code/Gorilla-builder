// src/utils/auth.ts
// Gorilla Builder Auth Gateway SDK

export interface GorillaUser {
  id: string;
  email: string;
  name: string;
  avatar: string;
  provider: string;
}

/**
 * Opens the secure Gorilla Auth Gateway in a popup window.
 */
export const login = (provider: 'google' | 'github' = 'google') => {
  // This is injected securely by the Python backend during generation and deployment
  const authId = import.meta.env.VITE_GORILLA_AUTH_ID;
  
  if (!authId) {
    console.error("Authentication failed: Missing VITE_GORILLA_AUTH_ID environment variable.");
    alert("Authentication is not configured for this app yet.");
    return;
  }

  // Center the popup on the user's screen
  const width = 450;
  const height = 600;
  const left = window.screen.width / 2 - width / 2;
  const top = window.screen.height / 2 - height / 2;
  
  // Point to the specific app-auth initiation route on the backend
  const authUrl = `https://slaw-carefully-cried.ngrok-free.dev/api/v1/app-auth/${authId}/${provider}?return_url=${encodeURIComponent(window.location.origin)}`;
  
  window.open(
    authUrl, 
    'GorillaAuthPopup', 
    `width=${width},height=${height},top=${top},left=${left},toolbar=no,menubar=no,scrollbars=no,resizable=no`
  );
};

/**
 * Listens for the successful login payload from the popup and caches it.
 * Should be used inside a React useEffect.
 */
export const onAuthStateChanged = (callback: (user: GorillaUser | null) => void) => {
  // 1. Check if the user is already logged in from a previous session
  const cached = localStorage.getItem('gorilla_app_user');
  if (cached) {
    try {
      callback(JSON.parse(cached));
    } catch (e) {
      console.error("Failed to parse cached user");
    }
  }

  // 2. Listen for the postMessage from the Gorilla Auth Gateway popup
  const listener = (event: MessageEvent) => {
    // Process only our specific auth success message
    if (event.data && event.data.type === 'GORILLA_AUTH_SUCCESS') {
      const user = event.data.payload as GorillaUser;
      localStorage.setItem('gorilla_app_user', JSON.stringify(user));
      callback(user);
    }
  };

  window.addEventListener('message', listener);

  // Return an unsubscribe function to clean up the event listener
  return () => window.removeEventListener('message', listener);
};

/**
 * Clears the user session.
 */
export const logout = (callback?: () => void) => {
  localStorage.removeItem('gorilla_app_user');
  if (callback) {
    callback();
  } else {
    window.location.reload(); // Force a clean slate
  }
};