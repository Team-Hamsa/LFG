import { Buffer } from 'buffer';
if (typeof globalThis.Buffer === 'undefined') globalThis.Buffer = Buffer;
export { default as SignClient } from '@walletconnect/sign-client';
export { WalletConnectModal } from '@walletconnect/modal';
