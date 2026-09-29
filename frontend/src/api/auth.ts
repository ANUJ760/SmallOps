/// <reference types="vite/client" />
import { CognitoUserPool, CognitoUser, AuthenticationDetails, CognitoUserAttribute } from 'amazon-cognito-identity-js';

const poolData = {
  UserPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID || '',
  ClientId: import.meta.env.VITE_COGNITO_APP_CLIENT_ID || ''
};

export const userPool = new CognitoUserPool(poolData);

export function loginWithCognito(email: string, password: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!poolData.UserPoolId || !poolData.ClientId) {
      return reject(new Error("Cognito credentials missing from .env"));
    }

    const authenticationDetails = new AuthenticationDetails({
      Username: email,
      Password: password
    });

    const cognitoUser = new CognitoUser({
      Username: email,
      Pool: userPool
    });

    cognitoUser.authenticateUser(authenticationDetails, {
      onSuccess: (result) => {
        const token = result.getIdToken().getJwtToken();
        const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
        
        if (!isLocalhost) {
          localStorage.setItem('bb_token', token);
          localStorage.setItem('bb_user', email);
        } else {
          sessionStorage.setItem('bb_token', token);
          sessionStorage.setItem('bb_user', email);
        }
        resolve(token);
      },
      onFailure: (err) => {
        reject(err);
      },
      newPasswordRequired: (_userAttributes, _requiredAttributes) => {
        reject(new Error("New password required. Please reset password via AWS Console."));
      }
    });
  });
}

export function signUpWithCognito(email: string, password: string): Promise<any> {
  return new Promise((resolve, reject) => {
    if (!poolData.UserPoolId || !poolData.ClientId) {
      return reject(new Error("Cognito credentials missing from .env"));
    }
    const userAttributes = [
      new CognitoUserAttribute({
        Name: 'email',
        Value: email,
      }),
    ];
    userPool.signUp(email, password, userAttributes, null as any, (err, result) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(result);
    });
  });
}

export function confirmRegistration(email: string, code: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const cognitoUser = new CognitoUser({
      Username: email,
      Pool: userPool
    });
    cognitoUser.confirmRegistration(code, true, (err, result) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(result);
    });
  });
}

export function resendConfirmationCode(email: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const cognitoUser = new CognitoUser({
      Username: email,
      Pool: userPool
    });
    cognitoUser.resendConfirmationCode((err, result) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(result);
    });
  });
}

export function getValidToken(): Promise<string | null> {
  return new Promise((resolve) => {
    if (!poolData.UserPoolId || !poolData.ClientId) {
      const fallbackToken = localStorage.getItem('bb_token') || sessionStorage.getItem('bb_token');
      return resolve(fallbackToken);
    }

    const cognitoUser = userPool.getCurrentUser();
    if (!cognitoUser) {
      const fallbackToken = localStorage.getItem('bb_token') || sessionStorage.getItem('bb_token');
      if (fallbackToken) {
        try {
          const payload = JSON.parse(atob(fallbackToken.split('.')[1]));
          if (payload.exp && payload.exp * 1000 < Date.now()) {
            localStorage.removeItem('bb_token');
            sessionStorage.removeItem('bb_token');
            return resolve(null);
          }
        } catch {
          // If token format is not standard JWT, return fallback
        }
      }
      return resolve(fallbackToken);
    }

    cognitoUser.getSession((err: any, session: any) => {
      if (err || !session || !session.isValid()) {
        console.warn("Cognito session invalid or refresh failed:", err);
        localStorage.removeItem('bb_token');
        sessionStorage.removeItem('bb_token');
        return resolve(null);
      }

      const freshToken = session.getIdToken().getJwtToken();
      if (sessionStorage.getItem('bb_token')) {
        sessionStorage.setItem('bb_token', freshToken);
      } else {
        localStorage.setItem('bb_token', freshToken);
      }
      return resolve(freshToken);
    });
  });
}

export function logoutCognito(): void {
  const cognitoUser = userPool.getCurrentUser();
  if (cognitoUser) {
    cognitoUser.signOut();
  }
  localStorage.removeItem('bb_token');
  localStorage.removeItem('bb_user');
  sessionStorage.removeItem('bb_token');
  sessionStorage.removeItem('bb_user');
}


