import {downloadHandler} from './handler.mjs';
Deno.serve(downloadHandler({url:Deno.env.get('SUPABASE_URL'),
 publicKey:Deno.env.get('CONVOLAB_PUBLISHABLE_KEY'),secretKey:Deno.env.get('CONVOLAB_SECRET_KEY'),
 origin:Deno.env.get('CONVOLAB_WEBSITE_ORIGIN')}));
