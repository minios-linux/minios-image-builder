# Sistema e módulos

Selecione os módulos e o ambiente de inicialização para esta entrada.

## Filtros de módulos

**Carregar módulos** restringe o carregamento aos nomes de módulos ou intervalos correspondentes.
**Ignorar módulos** exclui os módulos correspondentes. As sugestões vêm dos módulos
detectados na fonte MiniOS selecionada.

Esses controles geram `load=FILTER` e `noload=FILTER`. Use apenas os formatos de filtro
suportados pelo initramfs do MiniOS.

## Ambiente de inicialização

Mantenha o padrão da imagem, inicie o desktop gráfico, use um console de texto ou
entre no modo de resgate. O modo texto e o modo de resgate são destinados à administração
e à solução de problemas, e não ao uso normal do desktop.

## Compatibilidade gráfica

O modo de compatibilidade adiciona a opção do kernel Linux `nomodeset`. Use-o quando o modo normal
de configuração do kernel impedir o sistema gráfico de iniciar. Isso pode reduzir
a resolução e a aceleração da tela.

## Montagem automática de disco

Habilite a montagem automática somente quando a sessão deve expor outros sistemas
de arquivos conectados após a inicialização.
