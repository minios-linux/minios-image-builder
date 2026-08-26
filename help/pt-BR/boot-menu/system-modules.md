# Sistema e módulos

Selecione os módulos e o ambiente de inicialização para esta entrada.

## Filtros de módulos

**Carregar módulos** restringe o carregamento para nomes ou intervalos de módulos correspondentes.
**Ignorar módulos** exclui módulos correspondentes. As sugestões vêm dos módulos
detectados na fonte MiniOS selecionada.

Esses controles geram `load=FILTER` e `noload=FILTER`. Use apenas os formatos de filtro
suportados pelo initramfs do MiniOS.

## Ambiente de inicialização

Mantenha o padrão da imagem, inicie o desktop gráfico, use um console de texto ou
entre no modo de resgate. O modo texto e o modo de resgate são destinados à administração
e à solução de problemas, em vez do uso normal do desktop.

## Compatibilidade gráfica

O modo de compatibilidade adiciona a opção do kernel Linux `nomodeset`. Use-o quando o modo normal
de configuração do kernel impedir o sistema gráfico de iniciar. Isso pode reduzir
a resolução da tela e a aceleração.

## Montagem automática de disco

Ative a montagem automática somente quando a sessão deve expor outros sistemas de arquivos conectados após a inicialização.
