# Roteiro de fala · Software Factory

Apresentação em português, preparada em 22/10/2026. Duração sugerida: 25 a 30 minutos com participação do público.

## 01. Qual é o seu projeto pessoal que envolve AI?

Abra espaço para duas ou três respostas. Peça que cada pessoa nomeie um projeto e o problema que ele resolve. Não apresente o Software Factory ainda. Sugestão: 60 a 90 segundos.

## 02. Desafio: Me conte uma tendência de AI que eu não sei

Ouça exemplos do público. Se fizer sentido, conecte a conversa à passagem de prompts isolados para processos com ferramentas, memória, avaliação e participação humana. Isso é uma ponte narrativa, não uma previsão estatística.

## 03. Como você vê um profissional do futuro em times de AI?

Recolha respostas antes de mostrar a próxima parte. Use as palavras do público para conectar o papel profissional ao desenho do processo: perguntar bem, definir critérios, revisar evidências e decidir quando avançar.

## 04. Workflow: do pedido à execução

Comece pelo fluxo de desenvolvimento. No planejamento, um pedido delimitado passa pelo Grill; um esforço maior pode usar Wayfinder para organizar decisões em tickets. O time revisa escopo, critérios e documentos antes de aprovar a versão. No protótipo, esse caminho é simulado. A integração do plano aprovado ao controlador ainda precisa ser implementada. O harness existente executa bootstrap, baseline, Planner, Builder, testes, revisão e finalização. Falhas de teste geram reparo e novo teste; bloqueios de revisão geram revisão da implementação, teste e nova revisão. A saída é ready_for_review. Este quadro antecipa o fluxo; as próximas telas detalham os métodos e depois o grafo de execução. Não confundir o planejamento técnico interno do Planner com o Grill anterior à aprovação.

## 05. Grilling: decisões em rodadas com dependências

A skill manda organizar a conversa como uma árvore de decisões. Na skill original, o modelo identifica a fronteira por julgamento, sem um motor determinístico de dependências. Fronteira é o conjunto das perguntas que já podem ser feitas sem presumir respostas pendentes. Pergunte a fronteira inteira em uma rodada e espere as respostas. Uma pergunta dependente fica para depois. Quando faltar um fato do ambiente, a skill orienta delegar a investigação a um subagente; só os ramos dependentes dessa investigação aguardam. A sessão termina quando não há decisões abertas ou pressupostos silenciosos, e a pessoa confirma o entendimento. Não se inicia a execução só porque a entrevista terminou.

## 06. Como uma resposta libera a próxima rodada

Explique o exemplo da esquerda para a direita. Só depois de decidir quem é dono da sessão podemos discutir sua retomada. Só depois de definir o momento da aprovação podemos definir a cópia que será entregue. Se uma resposta anterior mudar, as decisões dependentes precisam ser reavaliadas. Isso preserva o comportamento adaptativo da skill, em vez de convertê-la em um formulário fixo.

## 07. Protótipo: perguntas, respostas e discussão

Mostre que o protótipo já permite interagir com rodadas, sugestões e discussão. O agente e as respostas são simulados neste estudo. O browser ainda não está ligado ao controlador. As opções de Browser, BB e Codex são vistas de uma sessão de exemplo, não conexões reais. Não há geração livre de rodadas por modelo nesta página. A captura foi feita para esta apresentação.

## 08. Revisão do plano e aprovação das versões

No protótipo, Finish Grill prepara o plano. A implementação só começa depois de Approve plan & start implementation. A cópia aprovada permanece imutável mesmo se o plano for editado depois. A repetição do mesmo pedido retorna o mesmo run simulado. O estudo compara três maneiras de revisar o mesmo conteúdo. Na versão local, domain-modeling mantém vocabulário em CONTEXT.md e registra decisões relevantes em ADRs. Esses arquivos são propostos e simulados na interface.

## 09. Wayfinder: mapa de decisões e dependências

Wayfinder, também de Matt Pocock, serve para um esforço maior do que cabe em uma sessão. Primeiro nomeia o destino. Em seguida explora a amplitude das decisões, cria uma issue-mapa e tickets filhos com perguntas precisas. Cada ticket contém uma decisão ou investigação, não uma fatia de implementação. O mapa é um índice, com destino, notas, decisões já tomadas, áreas ainda pouco definidas e fora de escopo. A fronteira contém tickets abertos, sem bloqueio e sem responsável. Questões precisas podem ter ticket mesmo bloqueadas. O que ainda não pode ser formulado fica na névoa. A figura é explicativa, não uma captura de uma funcionalidade existente.

## 10. Tipos de ticket e como são resolvidos

Research chama a skill de pesquisa. Prototype exige participação humana para avaliar o artefato. Grilling chama as skills grilling e domain-modeling. Task executa um pré-requisito necessário à decisão. Antes de trabalhar, a sessão assume o ticket para que outras não o escolham. Ao resolver, registra a resposta em comentário, fecha o ticket e adiciona um resumo com link ao mapa. Novos tickets aparecem conforme a névoa fica precisa. Em regra, uma sessão resolve um ticket; pesquisa é a exceção. O issue tracker é a fonte de verdade, com fallback para Markdown local. Dependências nativas do tracker tornam a fronteira visível.

## 11. Fluxos de planejamento: Grill e Wayfinder

Wayfinder planeja por padrão. O mapa termina quando o caminho está claro, sem decisões pendentes para alcançar o destino. Só notas explícitas do esforço podem mudar essa regra. A pesquisa local descreve a continuação to-spec, to-tickets e implement. Essa sequência é o contexto de planejamento registrado no projeto, não uma chamada automática da skill Wayfinder. A ponte até o harness precisa respeitar o escopo aprovado e os documentos exatos. Uma sessão de Grill pode futuramente ser aberta a partir de um ticket Wayfinder.

## 12. Controlador, interfaces e workers

As interfaces enviam comandos e mostram registros. O controlador é dono do run ID, dos workers, do progresso e dos limites. LangGraph determina as transições. Python valida dados e executa verificações. Os workers nativos usam a conexão Codex, com proteção em containers na configuração macOS verificada. Fechar uma tela não equivale a cancelar uma execução. A memória persistente permite recuperação quando o serviço reinicia, segundo as regras de reconciliação. MCP é a conexão de ferramentas, não um modelo nem uma interface web. CLI e BB foram verificados com modelos reais. A ligação completa no desktop Codex ainda é trabalho futuro.

## 13. Workflow de execução e ciclos de correção

O grafo real começa com bootstrap e baseline. Depois executa Planner, Builder, testes, revisão e finalização. Reparos e revisões retornam aos testes. O modo single preserva o Reviewer único de configurações antigas; o focused usa Request, Code e Security com um Judge somente de leitura. O modo risk_based é opt-in e pode incluir Documentation Review. Se o orçamento acabar ou ocorrer um problema terminal, a execução termina como falha preservando os registros. A finalização confere a versão exata das evidências. ready_for_review não representa aprovação humana. A política opcional de entrega pode autorizar commit, push, PR e acompanhamento de CI. Merge e deploy ficam fora do workflow.

## 14. Persistência, recuperação e validação

A retomada não consiste em mandar o mesmo prompt de novo. O controlador verifica intenções de criação, operações e resultados existentes. Se a situação estiver incerta, pede recuperação. Os comandos de qualidade vêm de arrays argv revisados na configuração, não de comandos inventados pelo modelo. Existe um escritor por workspace. Cancelamento só é confirmado após a saída dos workers correspondentes. Essas propriedades tornam o processo inspecionável e limitam efeitos de falhas. Evite tratar isso como garantia de código correto ou de segurança absoluta.

## 15. Integração pendente: planejamento e execução

Este é o trabalho de integração pendente. O lado de planejamento já tem uma experiência demonstrável, mas simulada. O harness já executa mudanças reais. A linha tracejada é a integração que ainda precisamos construir. O browser deve enviar respostas e aprovações ao controlador, que valida, salva e agenda o próximo passo. A identidade do ticket do tracker permanece distinta da identidade da sessão e do run. A execução deve receber o escopo aprovado, sem disparar automaticamente ao terminar a entrevista.

## 16. Estado atual da implementação

Apresente separadamente o estado de implementação e a evidência de teste de cada parte. CLI e BB passaram fluxos reais. O servidor MCP existe e inspecionou execução real, mas continuação cruzada BB/MCP foi testada com workers simulados. O desktop Codex está adiado. O protótipo não salva após recarregar a página e não dispara workers reais. Wayfinder ainda não tem editor funcional nem integração de tracker no browser. Router e provedores externos também seguem adiados. As evidências de setembro são históricas, não uma nova certificação feita para os slides.

## 17. Frentes de trabalho para o time

Discuta a divisão de trabalho entre desenvolvimento, produto e qualidade. Uma contribuição pode começar sem código: executar o protótipo, comparar layouts, registrar uma confusão ou trazer uma tarefa real delimitada. Para engenharia, a próxima peça é a sessão de planejamento persistente e o handoff real. Para qualidade, priorize falhas de conexão, repetição e revisões desatualizadas. O segundo horizonte é Wayfinder ligado ao tracker. Estas são oportunidades propostas, não um roadmap com datas prometidas.

## 18. Próximas contribuições ao Software Factory

Feche com a discussão de trabalho do time. Escolher um caso pequeno, rodar o protótipo, registrar problemas e propor uma mudança são formas de começar. Para implementação, a próxima integração é salvar as rodadas e versões de planejamento no controlador e iniciar o run a partir do plano aprovado. Use a issue 10 para organizar a discussão. Mantenha o convite à participação vinculado a uma contribuição concreta.

## 19. Referências e evidências

Estas fontes sustentam a apresentação. As referências de Matt Pocock estão fixadas em commit para permitir revisão. As URLs do Software Factory apontam para a branch do protótipo, pois ele ainda é um experimento fora da main. Os diagramas de árvore e mapa são ilustrações didáticas. A interface é de fato o protótipo local capturado nesta preparação. A apresentação não publica issues, não inicia sessões Grilling ou Wayfinder e não executa workers de modelo.

