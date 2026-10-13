'use strict';
// Tokenize TeX before Markdown can consume its backslashes and underscores.
(() => {
  const escape = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const render = token => `<${token.display?'div':'span'} class="lecture-math">${token.display?'\\[':'\\('}${escape(token.tex)}${token.display?'\\]':'\\)'}</${token.display?'div':'span'}>`;
  marked.use({extensions:[{
    name:'lectureMathBlock',level:'block',
    start(src){return src.search(/\\\[|\$\$/);},
    tokenizer(src){
      const match=/^ {0,3}\\\[([\s\S]*?)\\\][ \t]*(?:\n|$)/.exec(src)||/^ {0,3}\$\$([\s\S]*?)\$\$[ \t]*(?:\n|$)/.exec(src);
      if(match)return {type:'lectureMathBlock',raw:match[0],tex:match[1].trim(),display:true};
    },renderer:render
  },{
    name:'lectureMathInline',level:'inline',
    start(src){return src.search(/\\[([]|\$/);},
    tokenizer(src){
      let match,display=false;
      if(src.startsWith('\\('))match=/^\\\(([\s\S]*?)\\\)/.exec(src);
      else if(src.startsWith('\\[')){match=/^\\\[([\s\S]*?)\\\]/.exec(src);display=true;}
      else if(src.startsWith('$$')){match=/^\$\$([\s\S]*?)\$\$/.exec(src);display=true;}
      else if(src.startsWith('$'))match=/^\$(?!\$)((?:\\.|[^\\$\n])+?)\$(?!\$)/.exec(src);
      if(match)return {type:'lectureMathInline',raw:match[0],tex:match[1],display};
    },renderer:render
  }]});
})();
