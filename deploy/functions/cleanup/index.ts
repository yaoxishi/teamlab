import {cleanupHandler} from './handler.mjs';
Deno.serve(cleanupHandler({url:Deno.env.get('SUPABASE_URL'),secretKey:Deno.env.get('CONVOLAB_SECRET_KEY'),
 cleanupSecret:Deno.env.get('CONVOLAB_CLEANUP_SECRET')}));
